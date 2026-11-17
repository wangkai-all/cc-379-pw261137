"""Sampler service: owns creation of runs and candidates.

Portfolio boundaries (documented in README):
  * the sampler reads source tables and writes runs/candidates;
  * it never stamps a candidate as published and never writes releases;
  * it asks the checker over HTTP for a verdict and records the answer.
"""

from __future__ import annotations

import json
import os
import sys

from ..db import audit, connect, init_db, tx
from ..http_api import ApiError, Router, serve
from ..util import http_json, utcnow, validate_waivers
from . import core
from . import demo as demo_actions

ROUTER = Router("sampler")


def _conn(services):
    return connect(services["db_path"])


@ROUTER.route("GET", "/api/health")
def health(services, body, query):
    return {"service": "sampler", "ok": True, "at": utcnow()}


@ROUTER.route("GET", "/api/config")
def get_config(services, body, query):
    conn = _conn(services)
    cfg = core.load_config(conn)
    return {"config": cfg, "fingerprint": core.config_fingerprint(cfg)}


@ROUTER.route("GET", "/api/frame")
def get_frame(services, body, query):
    conn = _conn(services)
    cfg = core.load_config(conn)
    seed = query.get("seed") or "preview"
    source = core.load_source(conn)
    built = core.build_frame(cfg, source, seed)
    strata = [{
        "stratum_id": sid,
        "eligible": len(entries),
        "min_rows": built["strata"][sid]["min_rows"],
    } for sid, entries in sorted(built["frame"].items())]
    return {"strata": strata, "ineligible": built["ineligible"]}


@ROUTER.route("POST", "/api/runs")
def create_run(services, body, query):
    conn = _conn(services)
    run = core.create_run(
        conn,
        label=str(body.get("label") or "untitled"),
        seed=body.get("seed", ""),
        sample_size=body.get("sample_size"),
        mode=str(body.get("mode") or "initial"),
        parent_run_id=body.get("parent_run_id") or None,
        salt=body.get("salt"),
    )
    return 201, {"run": run}


@ROUTER.route("GET", "/api/runs")
def list_runs(services, body, query):
    conn = _conn(services)
    rows = conn.execute(
        "SELECT run_id, label, seed, sample_size, mode, parent_run_id, state,"
        " created_at FROM runs ORDER BY created_at DESC, run_id DESC").fetchall()
    return {"runs": [dict(r) for r in rows]}


@ROUTER.route("GET", "/api/runs/{run_id}")
def get_run(services, body, query, run_id):
    conn = _conn(services)
    return {"run": core.get_run(conn, run_id)}


@ROUTER.route("POST", "/api/runs/{run_id}/candidates")
def build_candidate(services, body, query, run_id):
    conn = _conn(services)
    candidate = core.build_candidate(conn, run_id)
    return 201, {"candidate": candidate}


@ROUTER.route("GET", "/api/candidates/{candidate_id}")
def get_candidate(services, body, query, candidate_id):
    """Full candidate read.  Payload is included here (and only here) so the
    console can render member rows and the change ledger."""
    conn = _conn(services)
    return {"candidate": core.get_candidate(conn, candidate_id)}


@ROUTER.route("POST", "/api/candidates/{candidate_id}/check")
def check_candidate(services, body, query, candidate_id):
    """Ask the independent checker for a verdict; record but do not worsen it."""
    conn = _conn(services)
    candidate = core.get_candidate(conn, candidate_id)
    if candidate["state"] in ("published",):
        raise ApiError(409, "candidate already published; build a new candidate")
    waivers = validate_waivers(body.get("waivers"))
    url = services["checker_url"].rstrip("/") + f"/api/candidates/{candidate_id}/check"
    status, reply = http_json(url, method="POST", payload={
        "require_publishable": bool(body.get("require_publishable", False)),
        "waivers": waivers,
    }, timeout=float(services.get("checker_timeout", 30)))
    if status >= 500:
        raise ApiError(502, "checker unavailable", checker_status=status,
                       checker_reply=reply)
    if status == 404:
        raise ApiError(404, reply.get("error", "candidate not found by checker"))
    verdict = reply.get("verdict", "error")
    findings = reply.get("findings", [])
    core.record_check(conn, candidate_id, verdict, findings, waivers=waivers)
    with tx(conn):
        audit(conn, "sampler", "check", candidate_id,
              {"verdict": verdict, "findings": len(findings),
               "waived": len(reply.get("waived", []))})
    payload = {"candidate_id": candidate_id, "verdict": verdict,
               "findings": findings, "waived": reply.get("waived", []),
               "waiver_policy": reply.get("waiver_policy", []),
               "candidate": _public(core.get_candidate(conn, candidate_id))}
    return (200 if verdict == "pass" else 422), payload


@ROUTER.route("GET", "/api/source/changes")
def source_changes(services, body, query):
    conn = _conn(services)
    rows = conn.execute(
        "SELECT change_id, at, kind, target, detail FROM source_changes"
        " ORDER BY change_id DESC LIMIT 100").fetchall()
    return {"changes": [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]}


@ROUTER.route("POST", "/api/demo/{action}")
def demo(services, body, query, action):
    conn = _conn(services)
    handler = demo_actions.ACTIONS.get(action)
    if handler is None:
        raise ApiError(404, f"unknown demo action {action}")
    return handler(conn, services, body or {})


@ROUTER.route("GET", "/api/audit")
def audit_tail(services, body, query):
    conn = _conn(services)
    rows = conn.execute(
        "SELECT audit_id, at, actor, action, subject, detail FROM audit_log"
        " ORDER BY audit_id DESC LIMIT 200").fetchall()
    return {"audit": [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]}


def _public(candidate: dict) -> dict:
    return {k: v for k, v in candidate.items() if k != "payload"}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="ClusterProof sampler service")
    parser.add_argument("--host", default=os.environ.get("CLUSTERPROOF_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SAMPLER_PORT", "8711")))
    parser.add_argument("--db", default=os.environ.get("CLUSTERPROOF_DB",
                                                        "var/clusterproof.sqlite3"))
    parser.add_argument("--checker-url", default=os.environ.get("CHECKER_URL",
                                                                "http://127.0.0.1:8712"))
    args = parser.parse_args(argv)
    init_db(args.db)
    services = {"db_path": args.db, "checker_url": args.checker_url}
    serve("sampler", ROUTER, services, args.host, args.port)


if __name__ == "__main__":
    main()
