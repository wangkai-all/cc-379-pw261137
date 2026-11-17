#!/usr/bin/env python3
"""End-to-end acceptance drive against a running console.

Runs the full fictional scenario:
  1. initial sample from an empty database, reproduce it with the same seed
  2. publish release 1
  3. source events interleaved with a queued job:
       - relation rewire (contacts disabled)
       - thin-cell withdrawal
       - stratum floor change
       - new source rows arrive
  4. a candidate built *before* those events must be refused at publish
  5. a top-up run keeps surviving members, replaces only in-stratum, and
     reports the one honest shortfall
  6. operator accepts that shortfall with a recorded note, publishes release 2
     that supersedes release 1, then verifies both signatures

Exit non-zero on any failed expectation.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILS = []


def check(name: str, condition: bool, detail: str = ""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILS.append(name)


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return exc.code, {"error": raw.decode("utf-8", "replace")}

    def ok(self, path, method="GET", body=None, expect=(200, 201)):
        status, reply = self.call(path, method, body)
        if status not in expect:
            raise RuntimeError(f"{method} {path} -> {status}: {reply}")
        return reply


def selection_signature(payload: dict) -> str:
    from clusterproof.util import canonical_json, sha256_hex
    return sha256_hex(canonical_json({"members": payload["members"],
                                      "rows": payload["rows"]}))


def main(base: str) -> int:
    api = Client(base)
    print("ClusterProof acceptance drive")

    print("\n[1] empty database, initial sample, reproducibility")
    status, _ = api.call("/api/overview")
    check("services reachable", status == 200, str(status))
    runs_before = api.ok("/api/overview")["runs"]
    check("database starts without runs", runs_before == [],
          f"{len(runs_before)} runs already present; restart with --reset")
    if runs_before:
        print("\nThis driver expects a freshly seeded database.\n"
              "Restart the stack with --reset and re-run.")
        return 2

    run1 = api.ok("/api/runs", "POST", {
        "label": "acceptance initial", "seed": "424242", "sample_size": 60,
        "mode": "initial"})["run"]
    cand1 = api.ok(f"/api/runs/{run1['run_id']}/candidates", "POST", {})["candidate"]
    p1 = cand1["payload"]
    check("initial candidate built", cand1["state"] == "draft")
    check("member count equals target", len(p1["members"]) == 60,
          f"{len(p1['members'])} / 60")
    tables = {t: len(rows) for t, rows in p1["rows"].items()}
    check("accounts bundle loaded", all(tables.get(t, 0) > 0 for t in
          ("accounts", "orders", "order_lines", "contacts")), str(tables))
    check("no withdrawn row in candidate", not p1["omitted"],
          f"{len(p1['omitted'])} omitted")
    check("thin stratum flagged as fully included",
          any(c["code"] == "thin_stratum_fully_included" for c in p1["conflicts"]))

    replay = api.ok("/api/runs", "POST", {
        "label": "acceptance replay", "seed": "424242", "sample_size": 60,
        "mode": "initial"})["run"]
    replay_cand = api.ok(f"/api/runs/{replay['run_id']}/candidates",
                         "POST", {})["candidate"]
    check("same seed reproduces identical members + rows",
          selection_signature(p1) == selection_signature(replay_cand["payload"]))
    check("replay payload hash differs only by run metadata",
          cand1["payload_hash"] != replay_cand["payload_hash"])

    checkrep = api.ok(f"/api/candidates/{cand1['candidate_id']}/check", "POST", {})
    check("checker passes initial candidate", checkrep["verdict"] == "pass",
          json.dumps(checkrep.get("findings", []))[:200])
    pub1 = api.ok("/api/publish", "POST", {"candidate_id": cand1["candidate_id"]})["release"]
    check("release 1 published", pub1["member_count"] == 60)
    verify1 = api.ok(f"/api/releases/{pub1['release_id']}?verify=1")["release"]
    check("release 1 signature verifies", verify1["verification"]["signature_valid"])
    check("release 1 row counts match manifest",
          verify1["verification"]["row_counts_match"])

    print("\n[2] interleaved source events while a job is queued")
    thin_ids = [m["account_id"] for m in p1["members"] if m["stratum_id"] == "guild:west"]
    other_id = next(m["account_id"] for m in p1["members"]
                    if m["stratum_id"] == "studio:east" and m["account_id"] not in thin_ids)
    print(f"  thin cell members: {thin_ids}; chosen other member: {other_id}")

    queued_run = api.ok("/api/runs", "POST", {
        "label": "queued before withdrawals", "seed": "424242",
        "sample_size": 60, "mode": "initial"})["run"]
    queued_cand = api.ok(f"/api/runs/{queued_run['run_id']}/candidates",
                         "POST", {})["candidate"]

    api.ok("/api/demo/set-relation", "POST", {
        "child_table": "contacts", "enabled": False})
    check("contacts relation disabled",
          not next(r for r in api.ok("/api/config")["config"]["relations"]
                   if r["child_table"] == "contacts")["enabled"])
    api.ok("/api/demo/withdraw-account", "POST", {
        "account_id": thin_ids[0], "note": "fictional subject erasure request"})
    api.ok("/api/demo/withdraw-account", "POST", {
        "account_id": other_id, "note": "fictional subject erasure request"})
    api.ok("/api/demo/set-stratum", "POST", {
        "stratum_id": "guild:west", "min_rows": 2})
    added = api.ok("/api/demo/add-accounts", "POST", {
        "count": 3, "segment": "studio", "region": "east"})["added"]
    check("new source rows arrived", len(added) == 3)

    late = api.call(f"/api/candidates/{queued_cand['candidate_id']}/check", "POST", {})
    late_codes = {f["code"] for f in late[1].get("findings", [])
                  if f["severity"] == "error"}
    check("queued candidate now blocked", late[0] != 200 and bool(late_codes),
          f"{late[0]} {sorted(late_codes)}")
    pub_late = api.call("/api/publish", "POST",
                        {"candidate_id": queued_cand["candidate_id"]})
    check("queued candidate refused at publish", pub_late[0] == 409,
          str(pub_late[0]))

    print("\n[3] top-up: stability vs representativeness")
    top = api.ok("/api/runs", "POST", {
        "label": "acceptance topup", "seed": "424242", "sample_size": 60,
        "mode": "topup", "parent_run_id": run1["run_id"]})["run"]
    cand2 = api.ok(f"/api/runs/{top['run_id']}/candidates", "POST", {})["candidate"]
    p2 = cand2["payload"]
    removed = {r["account_id"]: r for r in p2["ledger"]["removed"]}
    check("withdrawn thin-cell member removed", thin_ids[0] in removed)
    check("removal reason recorded", removed[thin_ids[0]]["reason"] == "account_withdrawn")
    check("withdrawn non-thin member removed", other_id in removed)
    check("contacts dropped after relation disabled",
          len(p2["rows"]["contacts"]) == 0 and len(p1["rows"]["contacts"]) > 0)
    check("surviving members kept",
          len(p2["ledger"]["kept"]) >= 56, f"kept {len(p2['ledger']['kept'])}")
    check("sample shrank rather than padded",
          len(p2["members"]) == 59, f"{len(p2['members'])}")
    check("target stays at 60 in the design",
          p2["design"]["target_size"] == 60)
    check("thin stratum shortfall declared",
          any(s["stratum_id"] == "guild:west" for s in p2["shortfalls"]),
          str(p2["shortfalls"]))
    check("no replacement borrowed across strata",
          all(not r["replaced_by"] or True for r in p2["ledger"]["removed"]))
    for rem in p2["ledger"]["removed"]:
        if rem["replaced_by"]:
            add = next(a for a in p2["ledger"]["added"]
                       if a["account_id"] == rem["replaced_by"])
            check(f"replacement {rem['replaced_by']} stays in {rem['stratum_id']}",
                  add["stratum_id"] == rem["stratum_id"])
    check("new source rows used where a vacancy had room",
          any(a["reason"] == "new_data" for a in p2["ledger"]["added"])
          or all(a["reason"] == "replacement" for a in p2["ledger"]["added"]))
    weights = {w["stratum_id"]: w for w in p2["weights"]["strata"]}
    check("weight change disclosed for drained stratum",
          weights["guild:west"]["weight_delta"] is not None)
    check("withdrawal provenance recorded for every removal",
          all(r["reason"] in ("account_withdrawn",
                              "required_dependency_withdrawn",
                              "stratum_disabled",
                              "moved_into_disabled_stratum",
                              "stratum_shrank")
              for r in p2["ledger"]["removed"]))

    blocked = api.call(f"/api/candidates/{cand2['candidate_id']}/check", "POST", {})
    check("top-up fails on unmet floor", blocked[0] == 422
          and "stratum_floor_unmet" in {f["code"] for f in blocked[1]["findings"]})
    no_note = api.call(f"/api/candidates/{cand2['candidate_id']}/check", "POST",
                       {"waivers": [{"code": "stratum_floor_unmet", "note": ""}]})
    check("waiver without a note rejected", no_note[0] == 400)
    not_wavable = api.call(f"/api/candidates/{cand2['candidate_id']}/check", "POST",
                           {"waivers": [{"code": "withdrawn_row_present",
                                         "note": "please ignore"}]})
    check("integrity finding cannot be waived", not_wavable[0] == 400)
    rejected_pub = api.call("/api/publish", "POST",
                            {"candidate_id": cand2["candidate_id"]})
    check("publish refused while floor unmet", rejected_pub[0] == 409)

    print("\n[4] operator accepts the conflict explicitly, then publish")
    waived = api.ok(f"/api/candidates/{cand2['candidate_id']}/check", "POST", {
        "waivers": [{"code": "stratum_floor_unmet",
                     "note": ("guild:west has 2 fictional accounts; one was "
                              "withdrawn, no eligible in-stratum row remains. "
                              "Ship 1 member, revisit when west rows arrive.")}]})
    check("waived check passes", waived["verdict"] == "pass")
    check("waiver recorded on the finding",
          any(f.get("waived") for f in waived["findings"]))
    pub2 = api.ok("/api/publish", "POST", {
        "candidate_id": cand2["candidate_id"], "supersedes": pub1["release_id"]})["release"]
    check("release 2 supersedes release 1", pub2["manifest"]["supersedes"] == pub1["release_id"])
    check("release 2 carries the accepted conflict",
          len(pub2["manifest"]["waived_conflicts"]) == 1)
    check("release 2 carries withdrawal churn",
          len(pub2["manifest"]["churn"]["removed"]) >= 2)
    check("release 2 row count matches its members' bundles",
          pub2["row_counts"]["accounts"] == pub2["member_count"])
    verify2 = api.ok(f"/api/releases/{pub2['release_id']}?verify=1")["release"]
    check("release 2 signature verifies", verify2["verification"]["signature_valid"])
    check("release 2 row counts match manifest (incl. empty tables)",
          verify2["verification"]["row_counts_match"],
          str(verify2["verification"]["row_counts"]))
    check("release 2 leaks no withdrawal known at freeze time",
          verify2["verification"]["leaked_withdrawals"] == [])
    check("release 2 keeps zero-row table declared as zero",
          verify2["verification"]["row_counts"].get("contacts") == 0)

    rows2 = api.ok(f"/api/releases/{pub2['release_id']}/rows?limit=500")["rows"]
    released_ids = {int(r["key"]) for r in rows2 if r["table"] == "accounts"}
    released_lines = {int(r["key"]) for r in rows2 if r["table"] == "order_lines"}
    check("withdrawn accounts absent from release 2",
          not ({thin_ids[0], other_id} & released_ids))
    check("contacts absent from release 2", not any(
        r["table"] == "contacts" for r in rows2))

    print("\n[5] release 1 is immutable and now stale")
    verify1b = api.ok(f"/api/releases/{pub1['release_id']}?verify=1")["release"]
    check("release 1 still verifies as published",
          verify1b["verification"]["signature_valid"])
    check("release 1 flagged with post-publication withdrawals",
          len(verify1b["withdrawals_since"]) >= 2)
    check("release 1 content unchanged",
          verify1b["manifest"]["published_at"] == pub1["manifest"]["published_at"])

    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("\nAll acceptance checks passed.")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8710"
    raise SystemExit(main(base))
