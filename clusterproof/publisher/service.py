"""Publisher service HTTP surface and CLI.

Kept separate from the sampler process: it accepts no source mutations, only
publish requests, and can be exposed to a different audience.
"""

from __future__ import annotations

import os

from ..db import audit, connect, init_db, tx
from ..http_api import ApiError, Router, serve
from ..util import utcnow
from . import core

ROUTER = Router("publisher")


def _secret(services) -> str:
    return services["secret"]


@ROUTER.route("GET", "/api/health")
def health(services, body, query):
    return {"service": "publisher", "ok": True, "at": utcnow()}


@ROUTER.route("GET", "/api/releases")
def list_releases(services, body, query):
    conn = connect(services["db_path"])
    rows = conn.execute(
        "SELECT release_id, candidate_id, run_id, published_at, payload_hash,"
        " signature, supersedes FROM releases ORDER BY published_at DESC").fetchall()
    return {"releases": [dict(r) for r in rows]}


@ROUTER.route("GET", "/api/releases/{release_id}")
def get_release(services, body, query, release_id):
    conn = connect(services["db_path"])
    data = core.get_release(conn, release_id)
    if query.get("verify") == "1":
        data["verification"] = core.verify_release(conn, release_id, _secret(services))
    data["withdrawals_since"] = core.withdrawals_since(conn,
                                                       data["change_cursor"])
    return {"release": data}


@ROUTER.route("GET", "/api/releases/{release_id}/rows")
def get_rows(services, body, query, release_id):
    conn = connect(services["db_path"])
    return core.release_rows(conn, release_id, table=query.get("table"),
                             limit=int(query.get("limit", 200)))


@ROUTER.route("GET", "/api/releases/{release_id}/verify")
def verify(services, body, query, release_id):
    conn = connect(services["db_path"])
    return core.verify_release(conn, release_id, _secret(services))


@ROUTER.route("POST", "/api/publish")
def publish(services, body, query):
    candidate_id = str(body.get("candidate_id") or "")
    if not candidate_id:
        raise ApiError(400, "candidate_id is required")
    conn = connect(services["db_path"])
    try:
        result = core.publish(conn, candidate_id, _secret(services),
                              supersedes=body.get("supersedes"),
                              checker_report=body.get("checker_report"))
    except core.PublishError as exc:
        with tx(conn):
            audit(conn, "publisher", "publish_refused", candidate_id,
                  {"reason": str(exc)})
        raise ApiError(409, str(exc))
    return 201, {"release": result}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="ClusterProof sample publisher")
    parser.add_argument("--host", default=os.environ.get("CLUSTERPROOF_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PUBLISHER_PORT", "8713")))
    parser.add_argument("--db", default=os.environ.get("CLUSTERPROOF_DB",
                                                        "var/clusterproof.sqlite3"))
    parser.add_argument("--secret", default=os.environ.get(
        "CLUSTERPROOF_SIGNING_KEY", "fictional-demo-signing-key"))
    args = parser.parse_args(argv)
    init_db(args.db)
    serve("publisher", ROUTER,
          {"db_path": args.db, "secret": args.secret}, args.host, args.port)


if __name__ == "__main__":
    main()
