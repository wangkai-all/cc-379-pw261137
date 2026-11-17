"""Deterministic fictional source data and operator actions.

Every name, address and value here is invented for the demo. There is no
connection to a real customer system.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from .db import audit, tx
from .util import canonical_json, utcnow

SEGMENTS = ("solo", "studio", "guild")
REGIONS = ("north", "south", "east", "west")

# Deliberately thin cell: two accounts. With the default design quota of 3 it
# already reports a shortfall, and losing one member later breaks the floor of
# 2. That is the case where keeping sample size stable would require stealing
# rows from another stratum.
CELL_SIZES = {
    ("guild", "west"): 2,
    ("solo", "north"): 9,
    ("studio", "east"): 11,
}
DEFAULT_CELL_SIZE = 17

GIVEN = ("Ada", "Bram", "Cleo", "Dara", "Eli", "Faye", "Gio", "Hale", "Iris",
         "Jory", "Kai", "Lena", "Milo", "Nia", "Oren", "Pia", "Quin", "Rhea",
         "Sage", "Tovi", "Uma", "Vale", "Wren", "Xan", "Yara", "Zeph")
FAMILY = ("Alder", "Briar", "Cinder", "Dune", "Ember", "Fenwick", "Gale",
          "Harbor", "Ivory", "Juniper", "Kestrel", "Lumen", "Moss", "Nectar",
          "Orchid", "Pebble", "Quarry", "Reed", "Saffron", "Thistle",
          "Umber", "Verdant", "Willow", "Yarrow")
SKUS = ("TEA-01", "TEA-02", "MUG-11", "MUG-12", "BOX-30", "PEN-07", "LAMP-04")
CHANNELS = ("email", "sms", "post")


def _cell_counts() -> dict[tuple[str, str], int]:
    return {(s, r): CELL_SIZES.get((s, r), DEFAULT_CELL_SIZE)
            for s in SEGMENTS for r in REGIONS}


def _fictional_name(rng: random.Random) -> str:
    return f"{rng.choice(GIVEN)} {rng.choice(FAMILY)}"


def _add_account_bundle(conn, rng: random.Random, account_id: int,
                        segment: str, region: str, base_day: date) -> None:
    conn.execute(
        "INSERT INTO accounts(account_id, display_name, segment, region, status)"
        " VALUES (?,?,?,?,'active')",
        (account_id, _fictional_name(rng), segment, region),
    )
    n_orders = rng.randint(1, 4)
    for _ in range(n_orders):
        order_id = conn.execute("SELECT COALESCE(MAX(order_id),0)+1 FROM orders").fetchone()[0]
        placed = base_day - timedelta(days=rng.randint(0, 540))
        state = rng.choice(("paid", "shipped", "refunded"))
        conn.execute(
            "INSERT INTO orders(order_id, account_id, total_cents, placed_on, state)"
            " VALUES (?,?,?,?,?)",
            (order_id, account_id, 0, placed.isoformat(), state),
        )
        for _ in range(rng.randint(1, 3)):
            line_id = conn.execute("SELECT COALESCE(MAX(line_id),0)+1 FROM order_lines").fetchone()[0]
            unit = rng.choice((350, 900, 1250, 2400, 4800))
            qty = rng.randint(1, 3)
            conn.execute(
                "INSERT INTO order_lines(line_id, order_id, sku, qty, unit_cents)"
                " VALUES (?,?,?,?,?)",
                (line_id, order_id, rng.choice(SKUS), qty, unit),
            )
            conn.execute(
                "UPDATE orders SET total_cents = total_cents + ? WHERE order_id=?",
                (unit * qty, order_id),
            )
    for idx in range(rng.randint(1, 2)):
        contact_id = conn.execute(
            "SELECT COALESCE(MAX(contact_id),0)+1 FROM contacts").fetchone()[0]
        channel = CHANNELS[idx % len(CHANNELS)]
        conn.execute(
            "INSERT INTO contacts(contact_id, account_id, channel, value)"
            " VALUES (?,?,?,?)",
            (contact_id, account_id, channel,
             f"acct{account_id}.{channel}@example.test"),
        )
    for idx in range(rng.randint(0, 2)):
        note_id = conn.execute("SELECT COALESCE(MAX(note_id),0)+1 FROM notes").fetchone()[0]
        conn.execute(
            "INSERT INTO notes(note_id, account_id, body, created_on)"
            " VALUES (?,?,?,?)",
            (note_id, account_id, f"Fictional note {idx + 1} for account {account_id}.",
             (base_day - timedelta(days=rng.randint(1, 300))).isoformat()),
        )


def seed_fixtures(conn, reset: bool = False) -> dict:
    """Create the fictional source database and default design."""
    with tx(conn):
        if reset:
            for table in ("release_rows", "releases", "candidate_events", "candidates",
                          "runs", "source_changes", "strata", "relations",
                          "notes", "contacts", "order_lines", "orders", "accounts",
                          "audit_log"):
                conn.execute(f"DELETE FROM {table}")
        existing = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if existing:
            return {"seeded": False, "accounts": existing}
        rng = random.Random(20261117)
        base_day = date(2026, 10, 1)
        account_id = 1000
        for segment in SEGMENTS:
            for region in REGIONS:
                for _ in range(_cell_counts()[(segment, region)]):
                    _add_account_bundle(conn, rng, account_id, segment, region, base_day)
                    account_id += 1
        now = utcnow()
        for table, parent_table, parent_col, required, enabled in (
            ("orders", "accounts", "account_id", 1, 1),
            ("order_lines", "orders", "order_id", 1, 1),
            ("contacts", "accounts", "account_id", 0, 1),
            ("notes", "accounts", "account_id", 0, 1),
        ):
            conn.execute(
                "INSERT INTO relations(child_table, parent_table, parent_col,"
                " required, enabled, changed_at) VALUES (?,?,?,?,?,?)",
                (table, parent_table, parent_col, required, enabled, now),
            )
        # order_lines hang off orders rather than accounts; the parent column is
        # resolved against the parent row's account when the child is included.
        for segment in SEGMENTS:
            for region in REGIONS:
                conn.execute(
                    "INSERT INTO strata(stratum_id, segment, region, min_rows, enabled, changed_at)"
                    " VALUES (?,?,?,?,1,?)",
                    (f"{segment}:{region}", segment, region, 2, now),
                )
        audit(conn, "fixtures", "seed", "source", {"accounts": account_id - 1000})
    return {"seeded": True, "accounts": account_id - 1000}


def add_new_accounts(conn, count: int, segment: str, region: str, seed: int = 77) -> list[int]:
    """Operator action: append newly arrived source rows."""
    with tx(conn):
        rng = random.Random(seed)
        base_day = date(2026, 11, 10)
        mx = conn.execute("SELECT COALESCE(MAX(account_id),0) FROM accounts").fetchone()[0]
        ids = []
        for i in range(count):
            aid = mx + 1 + i
            _add_account_bundle(conn, rng, aid, segment, region, base_day)
            ids.append(aid)
        conn.execute(
            "INSERT INTO source_changes(at, kind, target, detail) VALUES (?,?,?,?)",
            (utcnow(), "insert", f"{segment}:{region}",
             canonical_json({"account_ids": ids})),
        )
        audit(conn, "operator", "add_accounts", f"{segment}:{region}", {"ids": ids})
    return ids


def withdraw_account(conn, account_id: int, note: str = "subject request") -> dict:
    """Operator action: mark one sensitive parent row withdrawn."""
    with tx(conn):
        row = conn.execute("SELECT status FROM accounts WHERE account_id=?",
                           (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"account {account_id} not found")
        if row["status"] == "withdrawn":
            raise ValueError(f"account {account_id} already withdrawn")
        now = utcnow()
        conn.execute(
            "UPDATE accounts SET status='withdrawn', withdrawn_at=?, withdrawal_note=?"
            " WHERE account_id=?",
            (now, note, account_id),
        )
        conn.execute(
            "INSERT INTO source_changes(at, kind, target, detail) VALUES (?,?,?,?)",
            (now, "withdrawal", f"account:{account_id}", canonical_json({"note": note})),
        )
        audit(conn, "operator", "withdraw", f"account:{account_id}", {"note": note})
    return {"account_id": account_id, "withdrawn_at": now}


def withdraw_child_row(conn, table: str, row_key: str, note: str = "subject request") -> dict:
    """Operator action: withdraw a sensitive child row (contact/order/note…)."""
    allowed = {"orders": "order_id", "order_lines": "line_id",
               "contacts": "contact_id", "notes": "note_id"}
    if table not in allowed:
        raise ValueError(f"cannot withdraw from {table}")
    col = allowed[table]
    with tx(conn):
        row = conn.execute(f"SELECT {col} AS k, withdrawn_at FROM {table} WHERE {col}=?",
                           (row_key,)).fetchone()
        if row is None:
            raise KeyError(f"{table} row {row_key} not found")
        if row["withdrawn_at"]:
            raise ValueError(f"{table} row {row_key} already withdrawn")
        now = utcnow()
        conn.execute(f"UPDATE {table} SET withdrawn_at=? WHERE {col}=?", (now, row_key))
        conn.execute(
            "INSERT INTO source_changes(at, kind, target, detail) VALUES (?,?,?,?)",
            (now, "withdrawal", f"{table}:{row_key}", canonical_json({"note": note})),
        )
        audit(conn, "operator", "withdraw", f"{table}:{row_key}", {"note": note})
    return {"table": table, "row_key": row_key, "withdrawn_at": now}


def set_relation(conn, child_table: str, *, enabled: bool | None = None,
                 required: bool | None = None) -> dict:
    """Operator action: rewire a dependency (enable/disable or require it)."""
    with tx(conn):
        row = conn.execute("SELECT * FROM relations WHERE child_table=?",
                           (child_table,)).fetchone()
        if row is None:
            raise KeyError(f"relation {child_table} not found")
        new_enabled = row["enabled"] if enabled is None else int(enabled)
        new_required = row["required"] if required is None else int(required)
        now = utcnow()
        conn.execute(
            "UPDATE relations SET enabled=?, required=?, changed_at=? WHERE child_table=?",
            (new_enabled, new_required, now, child_table),
        )
        conn.execute(
            "INSERT INTO source_changes(at, kind, target, detail) VALUES (?,?,?,?)",
            (now, "relation", child_table,
             canonical_json({"enabled": new_enabled, "required": new_required})),
        )
        audit(conn, "operator", "set_relation", child_table,
              {"enabled": new_enabled, "required": new_required})
    return {"child_table": child_table, "enabled": new_enabled,
            "required": new_required, "changed_at": now}


def set_stratum(conn, stratum_id: str, *, min_rows: int | None = None,
                enabled: bool | None = None) -> dict:
    """Operator action: change the stratification requirement."""
    with tx(conn):
        row = conn.execute("SELECT * FROM strata WHERE stratum_id=?",
                           (stratum_id,)).fetchone()
        if row is None:
            raise KeyError(f"stratum {stratum_id} not found")
        new_min = row["min_rows"] if min_rows is None else int(min_rows)
        new_enabled = row["enabled"] if enabled is None else int(enabled)
        now = utcnow()
        conn.execute(
            "UPDATE strata SET min_rows=?, enabled=?, changed_at=? WHERE stratum_id=?",
            (new_min, new_enabled, now, stratum_id),
        )
        conn.execute(
            "INSERT INTO source_changes(at, kind, target, detail) VALUES (?,?,?,?)",
            (now, "stratum", stratum_id,
             canonical_json({"min_rows": new_min, "enabled": new_enabled})),
        )
        audit(conn, "operator", "set_stratum", stratum_id,
              {"min_rows": new_min, "enabled": new_enabled})
    return {"stratum_id": stratum_id, "min_rows": new_min,
            "enabled": new_enabled, "changed_at": now}


def move_account(conn, account_id: int, segment: str, region: str) -> dict:
    """Operator action: a member's stratification attributes change."""
    if segment not in SEGMENTS or region not in REGIONS:
        raise ValueError("unknown segment/region")
    with tx(conn):
        row = conn.execute(
            "SELECT segment, region FROM accounts WHERE account_id=?",
            (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"account {account_id} not found")
        now = utcnow()
        conn.execute("UPDATE accounts SET segment=?, region=? WHERE account_id=?",
                     (segment, region, account_id))
        conn.execute(
            "INSERT INTO source_changes(at, kind, target, detail) VALUES (?,?,?,?)",
            (now, "stratum", f"account:{account_id}",
             canonical_json({"from": f"{row['segment']}:{row['region']}",
                             "to": f"{segment}:{region}"})),
        )
        audit(conn, "operator", "move_account", f"account:{account_id}",
              {"from": f"{row['segment']}:{row['region']}",
               "to": f"{segment}:{region}"})
    return {"account_id": account_id, "segment": segment, "region": region}


def demo_account_ids(conn) -> dict:
    """Convenient named picks for the scripted demo."""
    thin = conn.execute(
        "SELECT account_id FROM accounts WHERE segment='guild' AND region='west'"
        " AND status='active' ORDER BY account_id").fetchall()
    return {"thin_cell": [r["account_id"] for r in thin]}
