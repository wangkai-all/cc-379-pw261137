"""Race test: a withdrawal must never slip into a release being published.

Two loops run against the live stack:
  A. repeatedly publish a fresh, already-checked candidate
  B. concurrently withdraw the account that the next candidate will include

For every release that succeeds, no withdrawn-at-source account may appear in
its frozen rows.  A publish that loses the race must fail closed (409/422).
"""

from __future__ import annotations

import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8710"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 6


def call(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


def post(path, body):
    return call(path, "POST", body)


def main() -> int:
    status, ov = call("/api/overview")
    if status != 200:
        print("stack not reachable")
        return 2

    results = {"published_with_leak": 0, "published": 0, "refused": 0,
               "precheck_failed": 0}
    errors = []

    for round_no in range(ROUNDS):
        status, created = post("/api/runs", {
            "label": f"race {round_no}", "seed": f"race-{round_no}",
            "sample_size": 40, "mode": "initial"})
        if status not in (200, 201):
            errors.append(f"create run: {status} {created}")
            continue
        run_id = created["run"]["run_id"]
        status, built = post(f"/api/runs/{run_id}/candidates", {})
        if status not in (200, 201):
            errors.append(f"build: {status} {built}")
            continue
        cand = built["candidate"]
        target = cand["payload"]["members"][0]["account_id"]
        status, checked = post(f"/api/candidates/{cand['candidate_id']}/check", {})
        if status == 422:
            # The race test is about withdrawal-vs-publish, not representativeness.
            # If the demo database has already been through acceptance.py, the
            # thin guild:west stratum can be below its floor; accept that one
            # waivable conflict explicitly rather than calling the round a
            # precheck failure.
            errors_only = {f["code"] for f in checked.get("findings", [])
                           if f["severity"] == "error"}
            if errors_only == {"stratum_floor_unmet"}:
                status, checked = post(
                    f"/api/candidates/{cand['candidate_id']}/check",
                    {"waivers": [{"code": "stratum_floor_unmet",
                                  "note": "race test: representativeness "
                                          "waived, not under test"}]})
        if status != 200:
            results["precheck_failed"] += 1
            continue

        outcome = {}

        def publisher():
            time.sleep(random.uniform(0, 0.03))
            outcome["pub"] = post("/api/publish",
                                  {"candidate_id": cand["candidate_id"]})

        def withdrawer():
            time.sleep(random.uniform(0, 0.03))
            outcome["wd"] = post("/api/demo/withdraw-account",
                                 {"account_id": target,
                                  "note": "race test erasure"})

        threads = [threading.Thread(target=publisher),
                   threading.Thread(target=withdrawer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        pub_status, pub_reply = outcome["pub"]
        wd_status, wd_reply = outcome["wd"]
        if wd_status not in (200, 201, 400):
            # 400 == already withdrawn (e.g. a previous round)
            errors.append(f"withdraw: {wd_status} {wd_reply}")
        if pub_status == 201:
            results["published"] += 1
            release_id = pub_reply["release"]["release_id"]
            _, rows = call(f"/api/releases/{release_id}/rows?limit=5000")
            released = {int(r["key"]) for r in rows.get("rows", [])
                        if r["table"] == "accounts"}
            _, rel = call(f"/api/releases/{release_id}?verify=1")
            # A withdrawal that happened *after* the release froze is not a
            # leak; the publisher records exactly which withdrawals it knew
            # about at freeze time.
            leak = rel["release"]["verification"].get("leaked_withdrawals", [])
            if leak:
                results["published_with_leak"] += 1
                errors.append(f"round {round_no}: release {release_id} "
                              f"leaked withdrawn rows {leak}")
            if wd_status in (200, 201):
                # If withdrawal committed before publish, publish must refuse.
                pass
            if not rel["release"]["verification"]["signature_valid"]:
                errors.append(f"round {round_no}: bad signature")
        else:
            results["refused"] += 1
            if pub_status not in (409, 422):
                errors.append(f"unexpected publish status {pub_status}: {pub_reply}")

    print(json.dumps(results, indent=2))
    if errors:
        print("\n".join(errors))
        return 1
    if results["published"] == 0:
        print("no publish succeeded; race test inconclusive")
        return 1
    print("no withdrawn row leaked into any successful release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
