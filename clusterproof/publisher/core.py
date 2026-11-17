"""Publisher: freezes a candidate into an immutable release.

Publishing is the only thing this service does.  It re-validates against the
live source inside a write transaction, so a withdrawal that lands while an
old job is queued cannot be re-emitted: the release row is copied only after
the check and the copy happen under the same lock.
"""

from __future__ import annotations

import json

from ..checker import checks as checker_checks
from ..db import audit, tx
from ..sampler import core
from ..util import canonical_json, sha256_hex, sign, short_hash, utcnow


class PublishError(ValueError):
    pass


def _release_manifest(release_id: str, candidate: dict, payload_hash: str,
                      supersedes: str | None) -> dict:
    payload = candidate["payload"]
    by_table = {t: len(rows) for t, rows in payload["rows"].items()}
    manifest = {
        "release_id": release_id,
        "candidate_id": candidate["candidate_id"],
        "run_id": candidate["run_id"],
        "published_at": None,
        "payload_hash": payload_hash,
        "row_counts": by_table,
        "member_count": len(payload["members"]),
        "target_size": payload["design"]["target_size"],
        "assignment": [
            {"stratum_id": a["stratum_id"], "population": a["population"],
             "quota": a["quota"], "selected": a.get("selected"),
             "weight": (round(a["population"] / a["selected"], 6)
                        if a.get("selected") else None)}
            for a in payload["design"]["allocation"]
        ],
        "churn": {
            "kept": len(payload["ledger"]["kept"]),
            "added": payload["ledger"]["added"],
            "removed": payload["ledger"]["removed"],
            "rehomed": payload["ledger"].get("rehomed", []),
        },
        "shortfalls": payload.get("shortfalls", []),
        "conflicts": payload.get("conflicts", []),
        "selector": payload.get("selector"),
        "config_fingerprint": payload.get("config_fingerprint"),
        "supersedes": supersedes,
        "withdrawal_rule": ("withdrawn source rows are never copied into a "
                            "release, even when the candidate was built earlier"),
    }
    return manifest


def publish(conn, candidate_id: str, secret: str, supersedes: str | None = None,
            checker_report: dict | None = None) -> dict:
    row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?",
                       (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(f"candidate {candidate_id} not found")
    candidate = dict(row)
    candidate["payload"] = json.loads(candidate["payload"])

    existing = conn.execute("SELECT release_id FROM releases WHERE candidate_id=?",
                            (candidate_id,)).fetchone()
    if existing:
        raise PublishError(f"candidate already published as {existing['release_id']}")

    # The publisher never trusts a caller-supplied verdict: it re-runs the
    # checker itself, against live source, inside the publish transaction.
    if checker_report is not None and checker_report.get("verdict") != "pass":
        raise PublishError("checker report attached to the request did not pass")

    if supersedes:
        if conn.execute("SELECT 1 FROM releases WHERE release_id=?",
                        (supersedes,)).fetchone() is None:
            raise PublishError(f"supersedes target {supersedes} does not exist")

    now = utcnow()
    payload_hash = candidate["payload_hash"]
    release_id = "rel-" + short_hash(candidate_id, payload_hash, now)[:16]
    manifest = _release_manifest(release_id, candidate, payload_hash, supersedes)

    # Everything below runs under one IMMEDIATE transaction.  A withdrawal that
    # commits before this point is caught by re-checking inside the lock; one
    # that commits after is simply outside this release.  Waivers recorded on
    # the candidate's check event are re-applied so an accepted representativeness
    # trade-off does not block, while integrity errors can never be waived.
    with tx(conn):
        waivers = _recorded_waivers(conn, candidate_id)
        recheck = checker_checks.check_candidate(
            conn, dict(row), core.load_config(conn), waivers=waivers)
        if recheck["verdict"] != "pass":
            raise PublishError(
                "source changed during publish; candidate is no longer valid: " +
                ", ".join(sorted({f["code"] for f in recheck["findings"]
                                  if f["severity"] == "error"})))

        manifest["published_at"] = now
        manifest["waived_conflicts"] = recheck.get("waived", [])
        # Record which withdrawals were already visible at freeze time, so an
        # auditor can distinguish "was clean, later erased" from "leaked".
        manifest["withdrawals_known_at_publish"] = [
            {"at": w["at"], "target": w["target"]} for w in conn.execute(
                "SELECT at, target FROM source_changes"
                " WHERE kind='withdrawal' ORDER BY change_id")
        ]
        manifest["withdrawal_rule"] = (
            "no withdrawal source_change at or before freeze may name a row in "
            "release_rows; later erasures are disclosed on the release page and "
            "never edit this release")
        body = canonical_json(manifest)
        signature = sign(secret, body)

        conn.execute(
            "INSERT INTO releases(release_id, candidate_id, run_id, published_at,"
            " payload_hash, signature, manifest, supersedes)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (release_id, candidate_id, candidate["run_id"], now, payload_hash,
             signature, body, supersedes),
        )
        inserted = 0
        for table, rows in candidate["payload"]["rows"].items():
            col = core.KEY_COLUMN[table]
            for item in rows:
                stratum_id = None
                if table == "accounts":
                    stratum_id = f"{item['segment']}:{item['region']}"
                elif table == "orders":
                    stratum_id = _account_stratum(candidate, item["account_id"])
                elif table == "order_lines":
                    order = _find_row(candidate, "orders", "order_id", item["order_id"])
                    if order:
                        stratum_id = _account_stratum(candidate, order["account_id"])
                else:
                    stratum_id = _account_stratum(candidate, item["account_id"])
                conn.execute(
                    "INSERT INTO release_rows(release_id, table_name, row_key,"
                    " row_json, stratum_id) VALUES (?,?,?,?,?)",
                    (release_id, table, str(item[col]), canonical_json(item),
                     stratum_id),
                )
                inserted += 1
        conn.execute("UPDATE candidates SET state='published' WHERE candidate_id=?",
                     (candidate_id,))
        conn.execute("UPDATE runs SET state='published' WHERE run_id=?",
                     (candidate["run_id"],))
        conn.execute(
            "INSERT INTO candidate_events(candidate_id, at, kind, detail)"
            " VALUES (?,?,?,?)",
            (candidate_id, now, "published", canonical_json({
                "release_id": release_id, "rows": inserted,
                "signature": signature,
            })),
        )
        audit(conn, "publisher", "publish", release_id, {
            "candidate_id": candidate_id, "rows": inserted,
            "supersedes": supersedes,
        })

    return {
        "release_id": release_id,
        "candidate_id": candidate_id,
        "published_at": now,
        "row_counts": manifest["row_counts"],
        "member_count": manifest["member_count"],
        "manifest_hash": sha256_hex(body),
        "signature": signature,
        "manifest": manifest,
    }


def _recorded_waivers(conn, candidate_id: str) -> list[dict]:
    """Waivers an operator attached when this candidate was last checked."""
    row = conn.execute(
        "SELECT detail FROM candidate_events WHERE candidate_id=? AND kind='check'"
        " ORDER BY event_id DESC LIMIT 1", (candidate_id,)).fetchone()
    if row is None:
        return []
    detail = json.loads(row["detail"])
    return [{k: v for k, v in w.items() if k in ("code", "note", "at")}
            for w in detail.get("waivers", [])]


def _account_stratum(candidate: dict, account_id: int):
    for m in candidate["payload"]["members"]:
        if m["account_id"] == account_id:
            return m["stratum_id"]
    return None


def _find_row(candidate: dict, table: str, col: str, value):
    for r in candidate["payload"]["rows"].get(table, []):
        if r[col] == value:
            return r
    return None


def verify_release(conn, release_id: str, secret: str) -> dict:
    row = conn.execute("SELECT * FROM releases WHERE release_id=?",
                       (release_id,)).fetchone()
    if row is None:
        raise KeyError(f"release {release_id} not found")
    expected = sign(secret, row["manifest"])
    ok = expected == row["signature"]
    manifest = json.loads(row["manifest"])
    row_counts = {}
    for count_row in conn.execute(
            "SELECT table_name, COUNT(*) AS n FROM release_rows"
            " WHERE release_id=? GROUP BY table_name ORDER BY table_name",
            (release_id,)):
        row_counts[count_row["table_name"]] = count_row["n"]
    # Tables that ended up empty produce no group; they must still appear so
    # the comparison against the manifest is meaningful.
    for table in manifest.get("row_counts", {}):
        row_counts.setdefault(table, 0)
    # Durable invariant, checked against the *frozen* manifest record rather
    # than wall-clock time: no row named in a withdrawal known at publish may
    # appear in this release.
    table_alias = {"account": "accounts"}
    leaked = []
    for change in manifest.get("withdrawals_known_at_publish", []):
        target = change.get("target", "")
        table, _, key = target.partition(":")
        table = table_alias.get(table, table)
        if table not in ("accounts", "orders", "order_lines", "contacts", "notes"):
            continue
        if not key.isdigit():
            continue
        hit = conn.execute(
            "SELECT 1 FROM release_rows WHERE release_id=? AND table_name=?"
            " AND row_key=?", (release_id, table, key)).fetchone()
        if hit:
            leaked.append(target)
    return {
        "release_id": release_id,
        "signature_valid": ok,
        "manifest_hash": sha256_hex(row["manifest"]),
        "row_counts_match": row_counts == manifest["row_counts"],
        "leaked_withdrawals": leaked,
        "row_counts": row_counts,
        "published_at": row["published_at"],
        "payload_hash": row["payload_hash"],
        "manifest": manifest,
    }


def get_release(conn, release_id: str) -> dict:
    row = conn.execute("SELECT * FROM releases WHERE release_id=?",
                       (release_id,)).fetchone()
    if row is None:
        raise KeyError(f"release {release_id} not found")
    manifest = json.loads(row["manifest"])
    counts = {r["table_name"]: r["n"] for r in conn.execute(
        "SELECT table_name, COUNT(*) AS n FROM release_rows WHERE release_id=?"
        " GROUP BY table_name", (release_id,))}
    # Zero-row tables leave no rows to group by; the manifest still declares
    # them, so surface a 0 rather than a missing key.
    for table in manifest.get("row_counts", {}):
        counts.setdefault(table, 0)
    return {
        "release_id": release_id,
        "candidate_id": row["candidate_id"],
        "run_id": row["run_id"],
        "published_at": row["published_at"],
        "payload_hash": row["payload_hash"],
        "signature": row["signature"],
        "supersedes": row["supersedes"],
        "change_cursor": row["change_cursor"],
        "manifest": manifest,
        "row_counts": counts,
    }


def release_rows(conn, release_id: str, table: str | None = None,
                 limit: int = 200) -> dict:
    if conn.execute("SELECT 1 FROM releases WHERE release_id=?",
                    (release_id,)).fetchone() is None:
        raise KeyError(f"release {release_id} not found")
    sql = ("SELECT table_name, row_key, stratum_id, row_json FROM release_rows"
           " WHERE release_id=?")
    params = [release_id]
    if table:
        sql += " AND table_name=?"
        params.append(table)
    sql += " ORDER BY table_name, CAST(row_key AS INTEGER) LIMIT ?"
    params.append(int(limit))
    rows = [{"table": r["table_name"], "key": r["row_key"],
             "stratum_id": r["stratum_id"], "row": json.loads(r["row_json"])}
            for r in conn.execute(sql, params)]
    return {"release_id": release_id, "rows": rows}


def withdrawals_since(conn, since_cursor: int) -> list[dict]:
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in conn.execute(
        "SELECT at, target, detail FROM source_changes WHERE kind='withdrawal'"
        " AND change_id > ? ORDER BY change_id", (int(since_cursor or 0),))]
