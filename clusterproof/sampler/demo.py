"""Scripted source-side actions used by the demo console.

These mutate *source* data only (withdrawals, new rows, relation and stratum
design changes).  They intentionally live behind the sampler service because
that service owns the source-facing side; the checker and publisher have no
write access to source tables.
"""

from __future__ import annotations

import json

from .. import fixtures
from ..db import audit, tx
from ..util import canonical_json


def _ok(**kw):
    return {"ok": True, **kw}


def seed(conn, services, body):
    reset = bool(body.get("reset"))
    result = fixtures.seed_fixtures(conn, reset=reset)
    return _ok(seed=result, demo=fixtures.demo_account_ids(conn))


def withdraw_account(conn, services, body):
    account_id = int(body["account_id"])
    result = fixtures.withdraw_account(conn, account_id,
                                       str(body.get("note") or "subject request"))
    latest = conn.execute(
        "SELECT release_id FROM releases ORDER BY published_at DESC LIMIT 1").fetchone()
    result["latest_release"] = latest["release_id"] if latest else None
    return _ok(withdrawal=result)


def withdraw_child(conn, services, body):
    result = fixtures.withdraw_child_row(
        conn, str(body["table"]), str(body["row_key"]),
        str(body.get("note") or "subject request"))
    return _ok(withdrawal=result)


def add_accounts(conn, services, body):
    count = int(body.get("count", 3))
    segment = str(body.get("segment") or "studio")
    region = str(body.get("region") or "east")
    ids = fixtures.add_new_accounts(conn, count, segment, region,
                                    seed=int(body.get("seed", 77)))
    return _ok(added=ids, segment=segment, region=region)


def set_relation(conn, services, body):
    result = fixtures.set_relation(
        conn, str(body["child_table"]),
        enabled=body.get("enabled"), required=body.get("required"))
    return _ok(relation=result)


def set_stratum(conn, services, body):
    result = fixtures.set_stratum(
        conn, str(body["stratum_id"]),
        min_rows=body.get("min_rows"), enabled=body.get("enabled"))
    return _ok(stratum=result)


def move_account(conn, services, body):
    result = fixtures.move_account(conn, int(body["account_id"]),
                                   str(body["segment"]), str(body["region"]))
    return _ok(move=result)


ACTIONS = {
    "seed": seed,
    "withdraw-account": withdraw_account,
    "withdraw-child": withdraw_child,
    "add-accounts": add_accounts,
    "set-relation": set_relation,
    "set-stratum": set_stratum,
    "move-account": move_account,
}
