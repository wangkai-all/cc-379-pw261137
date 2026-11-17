"""Sample console: a read-mostly web UI over the three service APIs.

The console holds no database connection.  It proxies exactly the calls an
operator needs and renders the conflict between "keep the same members" and
"keep the strata representative" instead of hiding it behind a fixed row count.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlencode

from ..http_api import ApiError, Router, serve
from ..util import http_json, utcnow

ROUTER = Router("console")
ROUTER.static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _services(name: str, services: dict) -> str:
    urls = services["upstream"]
    if name not in urls:
        raise ApiError(500, f"missing upstream {name}")
    return urls[name].rstrip("/")


def _call(services, service: str, path: str, method: str = "GET", payload=None):
    url = _services(service, services) + path
    status, reply = http_json(url, method=method, payload=payload)
    if status >= 400 and status not in (409, 422):
        raise ApiError(status, f"{service} said: " + str(reply))
    return status, reply


@ROUTER.route("GET", "/api/health")
def health(services, body, query):
    out = {"console": True, "at": utcnow(), "upstream": {}}
    for name in ("sampler", "checker", "publisher"):
        try:
            status, reply = _call(services, name, "/api/health")
            out["upstream"][name] = {"ok": status == 200, "detail": reply}
        except ApiError as exc:
            out["upstream"][name] = {"ok": False, "detail": exc.message}
    return out


@ROUTER.route("GET", "/api/config")
def get_config(services, body, query):
    return _call(services, "sampler", "/api/config")[1]


@ROUTER.route("GET", "/api/overview")
def overview(services, body, query):
    """Everything the console needs for its first paint."""
    _, cfg = _call(services, "sampler", "/api/config")
    _, frame = _call(services, "sampler", "/api/frame")
    _, runs = _call(services, "sampler", "/api/runs")
    _, releases = _call(services, "publisher", "/api/releases")
    _, changes = _call(services, "sampler", "/api/source/changes")
    _, rules = _call(services, "checker", "/api/rules")
    return {
        "design": cfg,
        "frame": frame,
        "runs": runs.get("runs", []),
        "releases": releases.get("releases", []),
        "source_changes": changes.get("changes", []),
        "checker_rules": rules,
        "at": utcnow(),
    }


@ROUTER.route("GET", "/api/runs")
def list_runs(services, body, query):
    return _call(services, "sampler", "/api/runs")[1]


@ROUTER.route("GET", "/api/runs/{run_id}")
def run_detail(services, body, query, run_id):
    _, run = _call(services, "sampler", f"/api/runs/{run_id}")
    candidates = []
    for meta in run["run"].get("candidates", []):
        _, detail = _call(services, "sampler",
                          f"/api/candidates/{meta['candidate_id']}")
        cand = detail["candidate"]
        reports = []
        for event in cand.get("events", []):
            if event["kind"] == "check":
                reports.append(event["detail"])
            if event["kind"] == "published":
                reports.append({"kind": "published", **event["detail"]})
        _, check = _call(services, "checker",
                         f"/api/candidates/{meta['candidate_id']}")
        candidates.append({**cand, "checks": reports,
                           "live_check": check.get("check")})
    return {"run": run["run"], "candidates": candidates}


@ROUTER.route("POST", "/api/runs")
def create_run(services, body, query):
    return _call(services, "sampler", "/api/runs", "POST", body)


@ROUTER.route("POST", "/api/runs/{run_id}/candidates")
def build_candidate(services, body, query, run_id):
    return _call(services, "sampler", f"/api/runs/{run_id}/candidates",
                 "POST", body)


@ROUTER.route("POST", "/api/candidates/{candidate_id}/check")
def check_candidate(services, body, query, candidate_id):
    return _call(services, "sampler",
                 f"/api/candidates/{candidate_id}/check", "POST", body)


@ROUTER.route("POST", "/api/publish")
def publish(services, body, query):
    return _call(services, "publisher", "/api/publish", "POST", body)


@ROUTER.route("GET", "/api/releases")
def list_releases(services, body, query):
    return _call(services, "publisher", "/api/releases")[1]


@ROUTER.route("GET", "/api/releases/{release_id}")
def release_detail(services, body, query, release_id):
    _, rel = _call(services, "publisher",
                   f"/api/releases/{release_id}?verify=1")
    _, rows = _call(services, "publisher",
                    f"/api/releases/{release_id}/rows?limit=120")
    return {"release": rel["release"], "rows": rows.get("rows", [])}


@ROUTER.route("GET", "/api/releases/{release_id}/rows")
def release_rows_path(services, body, query, release_id):
    suffix = "?" + urlencode(query) if query else ""
    return _call(services, "publisher",
                 f"/api/releases/{release_id}/rows{suffix}")


@ROUTER.route("GET", "/api/release-rows")
def release_rows(services, body, query):
    release_id = query.get("release_id")
    if not release_id:
        raise ApiError(400, "release_id is required")
    suffix = "?" + urlencode({k: v for k, v in query.items() if k != "release_id"})
    return _call(services, "publisher",
                 f"/api/releases/{release_id}/rows{suffix}")


@ROUTER.route("POST", "/api/demo/{action}")
def demo(services, body, query, action):
    _, reply = _call(services, "sampler", f"/api/demo/{action}", "POST", body)
    return reply


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="ClusterProof sample console")
    parser.add_argument("--host", default=os.environ.get("CLUSTERPROOF_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CONSOLE_PORT", "8710")))
    parser.add_argument("--sampler-url", default=os.environ.get("SAMPLER_URL",
                                                                "http://127.0.0.1:8711"))
    parser.add_argument("--checker-url", default=os.environ.get("CHECKER_URL",
                                                                "http://127.0.0.1:8712"))
    parser.add_argument("--publisher-url", default=os.environ.get("PUBLISHER_URL",
                                                                  "http://127.0.0.1:8713"))
    args = parser.parse_args(argv)
    services = {"upstream": {"sampler": args.sampler_url,
                             "checker": args.checker_url,
                             "publisher": args.publisher_url}}
    serve("console", ROUTER, services, args.host, args.port)


if __name__ == "__main__":
    main()
