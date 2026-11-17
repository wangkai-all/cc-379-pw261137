"""Checker service: read-only over source data, writes only verdict events.

It re-runs the checks on every request; verdicts returned here are advisory
to the sampler, while the publisher independently re-runs the same checks
before issuing a release.
"""

from __future__ import annotations

import json
import os

from ..db import audit, connect, init_db, tx
from ..http_api import ApiError, Router, serve
from ..util import utcnow
from . import checks

ROUTER = Router("checker")


@ROUTER.route("GET", "/api/health")
def health(services, body, query):
    return {"service": "checker", "ok": True, "at": utcnow()}


@ROUTER.route("GET", "/api/rules")
def rules(services, body, query):
    conn = connect(services["db_path"])
    cfg = checks.core.load_config(conn)
    return {
        "checks": [
            {"code": "member_row_mismatch", "severity": "error"},
            {"code": "row_without_member", "severity": "error"},
            {"code": "orphan_child", "severity": "error"},
            {"code": "withdrawn_row_present", "severity": "error"},
            {"code": "required_dependency_missing", "severity": "error"},
            {"code": "stratum_floor_unmet", "severity": "error"},
            {"code": "stratum_label_wrong", "severity": "error"},
            {"code": "silent_size_drift", "severity": "error"},
            {"code": "dangling_replacement", "severity": "error"},
            {"code": "design_changed_since_build", "severity": "warning"},
            {"code": "source_changed_since_build", "severity": "warning"},
            {"code": "member_stability_tradeoff", "severity": "warning"},
        ],
        "design_fingerprint": checks.core.config_fingerprint(cfg),
    }


@ROUTER.route("GET", "/api/candidates/{candidate_id}")
def inspect(services, body, query, candidate_id):
    conn = connect(services["db_path"])
    row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?",
                       (candidate_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"candidate {candidate_id} not found")
    report = checks.check_candidate(conn, dict(row))
    return {"candidate_id": candidate_id, "check": report}


@ROUTER.route("POST", "/api/candidates/{candidate_id}/check")
def check(services, body, query, candidate_id):
    conn = connect(services["db_path"])
    row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?",
                       (candidate_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"candidate {candidate_id} not found")
    from ..util import validate_waivers
    try:
        waivers = validate_waivers(body.get("waivers"))
    except ValueError as exc:
        raise ApiError(400, str(exc))
    report = checks.check_candidate(conn, dict(row), waivers=waivers)
    if body.get("record", True):
        with tx(conn):
            audit(conn, "checker", "verify", candidate_id,
                  {"verdict": report["verdict"],
                   "errors": report["errors"], "warnings": report["warnings"]})
    status = 200 if report["verdict"] == "pass" else 422
    return status, report


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="ClusterProof relation checker")
    parser.add_argument("--host", default=os.environ.get("CLUSTERPROOF_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CHECKER_PORT", "8712")))
    parser.add_argument("--db", default=os.environ.get("CLUSTERPROOF_DB",
                                                        "var/clusterproof.sqlite3"))
    args = parser.parse_args(argv)
    init_db(args.db)
    serve("checker", ROUTER, {"db_path": args.db}, args.host, args.port)


if __name__ == "__main__":
    main()
