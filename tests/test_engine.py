"""Engine-level tests (no HTTP): determinism, closure, churn policy, safety.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clusterproof import fixtures
from clusterproof.checker import checks
from clusterproof.db import init_db
from clusterproof.sampler import core
from clusterproof.util import canonical_json, sha256_hex


def selection_sig(payload):
    return sha256_hex(canonical_json({"members": payload["members"],
                                      "rows": payload["rows"]}))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.conn = init_db(self.tmp.name)
        fixtures.seed_fixtures(self.conn)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + suffix)
            except FileNotFoundError:
                pass

    def build(self, seed="424242", size=60, mode="initial", parent=None,
              label="t"):
        run = core.create_run(self.conn, label, seed, size, mode,
                              parent_run_id=parent)
        candidate = core.build_candidate(self.conn, run["run_id"])
        candidate["run_id"] = run["run_id"]
        return candidate

    def reload(self, candidate_id):
        return dict(self.conn.execute(
            "SELECT * FROM candidates WHERE candidate_id=?",
            (candidate_id,)).fetchone())

    def check(self, candidate_id, waivers=None):
        return checks.check_candidate(self.conn, self.reload(candidate_id),
                                      waivers=waivers)


class DeterminismTests(Base):
    def test_same_seed_same_members_and_rows(self):
        a = self.build(label="a")
        b = self.build(label="b")
        self.assertEqual(selection_sig(a["payload"]),
                         selection_sig(b["payload"]))

    def test_different_seed_changes_membership(self):
        a = self.build(seed="1", label="a")
        b = self.build(seed="2", label="b")
        self.assertNotEqual(selection_sig(a["payload"]),
                            selection_sig(b["payload"]))

    def test_new_rows_do_not_reshuffle_existing_members(self):
        first = self.build(label="first")
        kept = {m["account_id"] for m in first["payload"]["members"]}
        fixtures.add_new_accounts(self.conn, 40, "studio", "east")
        again = self.build(label="again")
        members = {m["account_id"] for m in again["payload"]["members"]}
        # Most original members survive; only the seeded quota boundary moves.
        self.assertGreater(len(kept & members), 50)


class ClosureTests(Base):
    def test_each_member_has_its_children(self):
        cand = self.build()
        p = cand["payload"]
        by_account = {}
        for order in p["rows"]["orders"]:
            by_account.setdefault(order["account_id"], set()).add(order["order_id"])
        line_orders = {l["order_id"] for l in p["rows"]["order_lines"]}
        for member in p["members"]:
            aid = member["account_id"]
            self.assertIn(aid, by_account, "member without orders")
            self.assertTrue(by_account[aid] <= line_orders,
                            "order without lines")

    def test_no_orphan_children(self):
        cand = self.build()
        p = cand["payload"]
        account_ids = {a["account_id"] for a in p["rows"]["accounts"]}
        order_ids = {o["order_id"] for o in p["rows"]["orders"]}
        for order in p["rows"]["orders"]:
            self.assertIn(order["account_id"], account_ids)
        for line in p["rows"]["order_lines"]:
            self.assertIn(line["order_id"], order_ids)
        for k in ("contacts", "notes"):
            for row in p["rows"][k]:
                self.assertIn(row["account_id"], account_ids)

    def test_disabled_relation_is_not_emitted(self):
        fixtures.set_relation(self.conn, "notes", enabled=False)
        cand = self.build()
        self.assertEqual(cand["payload"]["rows"]["notes"], [])

    def test_required_child_withdrawn_makes_member_ineligible(self):
        first = self.build()
        # Withdraw every order line of one member -> required dependency gone.
        target = first["payload"]["members"][0]["account_id"]
        orders = [o["order_id"] for o in first["payload"]["rows"]["orders"]
                  if o["account_id"] == target]
        for order_id in orders:
            lines = [l["line_id"] for l in
                     self.conn.execute("SELECT line_id FROM order_lines"
                                       " WHERE order_id=?", (order_id,))]
            for line_id in lines:
                fixtures.withdraw_child_row(self.conn, "order_lines", line_id)
        for order_id in orders:
            fixtures.withdraw_child_row(self.conn, "orders", order_id)
        built = core.build_frame(core.load_config(self.conn),
                                 core.load_source(self.conn), "424242")
        ids = {m["account_id"] for entries in built["frame"].values()
               for m in entries}
        self.assertNotIn(target, ids)


class WithdrawalTests(Base):
    def test_withdrawn_account_never_in_frame(self):
        cand = self.build()
        victim = cand["payload"]["members"][3]["account_id"]
        fixtures.withdraw_account(self.conn, victim)
        again = self.build(label="again")
        self.assertNotIn(victim, {m["account_id"]
                                  for m in again["payload"]["members"]})
        self.assertTrue(all(r["account_id"] != victim
                            for r in again["payload"]["rows"]["accounts"]))

    def test_child_withdrawal_is_reported_for_surviving_member(self):
        cand = self.build()
        account = cand["payload"]["members"][0]["account_id"]
        contact = next(c for c in cand["payload"]["rows"]["contacts"]
                       if c["account_id"] == account)
        fixtures.withdraw_child_row(self.conn, "contacts", contact["contact_id"])
        again = self.build(label="again")
        omitted = [o for o in again["payload"]["omitted"]
                   if o["table"] == "contacts"
                   and o["row_key"] == contact["contact_id"]]
        self.assertTrue(omitted)
        self.assertEqual(omitted[0]["reason"], "withdrawn")
        self.assertNotIn(contact["contact_id"],
                         {c["contact_id"] for c in again["payload"]["rows"]["contacts"]})


class TopUpTests(Base):
    def test_topup_keeps_members_and_shrinks_honestly(self):
        first = self.build(label="first")
        thin = [m["account_id"] for m in first["payload"]["members"]
                if m["stratum_id"] == "guild:west"]
        fixtures.withdraw_account(self.conn, thin[0])
        top = self.build(mode="topup", parent=first["run_id"], label="top")
        p = top["payload"]
        self.assertEqual(len(p["members"]), 59)
        self.assertEqual(p["design"]["target_size"], 60)
        self.assertTrue(any(s["stratum_id"] == "guild:west"
                            for s in p["shortfalls"]))
        self.assertEqual(sum(1 for r in p["ledger"]["removed"]
                             if r["account_id"] == thin[0]), 1)

    def test_topup_never_replaces_across_strata(self):
        first = self.build(label="first")
        victim = next(m["account_id"] for m in first["payload"]["members"]
                      if m["stratum_id"] == "solo:north")
        fixtures.withdraw_account(self.conn, victim)
        top = self.build(mode="topup", parent=first["run_id"], label="top")
        removed = next(r for r in top["payload"]["ledger"]["removed"]
                       if r["account_id"] == victim)
        self.assertIsNotNone(removed["replaced_by"])
        add = next(a for a in top["payload"]["ledger"]["added"]
                   if a["account_id"] == removed["replaced_by"])
        self.assertEqual(add["stratum_id"], "solo:north")

    def test_topup_prefers_newly_arrived_rows(self):
        first = self.build(label="first")
        victim = next(m["account_id"] for m in first["payload"]["members"]
                      if m["stratum_id"] == "studio:east")
        fixtures.withdraw_account(self.conn, victim)
        new_ids = fixtures.add_new_accounts(self.conn, 2, "studio", "east")
        top = self.build(mode="topup", parent=first["run_id"], label="top")
        added = {a["account_id"]: a for a in top["payload"]["ledger"]["added"]}
        self.assertTrue(any(aid in added for aid in new_ids),
                        "new rows should be used before old ones")
        used_new = [added[aid] for aid in new_ids if aid in added]
        self.assertTrue(all(a["reason"] == "new_data" for a in used_new))

    def test_topup_target_change_rejected(self):
        first = self.build(label="first")
        with self.assertRaises(core.SamplingError):
            self.build(mode="topup", size=61, parent=first["run_id"], label="bad")

    def test_disabled_stratum_members_are_removed(self):
        first = self.build(label="first")
        fixtures.set_stratum(self.conn, "solo:west", enabled=False)
        top = self.build(mode="topup", parent=first["run_id"], label="top")
        reasons = {r["stratum_id"]: r["reason"]
                   for r in top["payload"]["ledger"]["removed"]}
        self.assertEqual(reasons.get("solo:west"), "stratum_disabled")
        self.assertFalse(any(m["stratum_id"] == "solo:west"
                             for m in top["payload"]["members"]))

    def test_stratum_floor_change_is_checked(self):
        first = self.build(label="first")
        fixtures.set_stratum(self.conn, "guild:east", min_rows=20)
        top = self.build(mode="topup", parent=first["run_id"], label="top")
        report = self.check(top["candidate_id"])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("stratum_floor_unmet", codes)

    def test_member_move_is_followed_with_disclosure(self):
        first = self.build(label="first")
        mover = next(m["account_id"] for m in first["payload"]["members"]
                     if m["stratum_id"] == "studio:east")
        fixtures.move_account(self.conn, mover, "solo", "north")
        top = self.build(mode="topup", parent=first["run_id"], label="top")
        rehomed = {r["account_id"]: r for r in top["payload"]["ledger"]["rehomed"]}
        self.assertIn(mover, rehomed)
        self.assertEqual(rehomed[mover]["to"], "solo:north")
        member = next(m for m in top["payload"]["members"]
                      if m["account_id"] == mover)
        self.assertEqual(member["stratum_id"], "solo:north")


class CheckerTests(Base):
    def test_clean_candidate_passes(self):
        cand = self.build()
        report = self.check(cand["candidate_id"])
        self.assertEqual(report["verdict"], "pass", json.dumps(report)[:400])

    def test_withdrawal_after_build_blocks_old_candidate(self):
        cand = self.build()
        fixtures.withdraw_account(self.conn,
                                  cand["payload"]["members"][0]["account_id"])
        report = self.check(cand["candidate_id"])
        self.assertEqual(report["verdict"], "fail")
        self.assertIn("withdrawn_row_present",
                      {f["code"] for f in report["findings"]})

    def test_disabled_relation_blocks_stale_candidate(self):
        cand = self.build()
        fixtures.set_relation(self.conn, "notes", enabled=False)
        report = self.check(cand["candidate_id"])
        self.assertEqual(report["verdict"], "fail")
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("disabled_relation_populated", codes)
        self.assertNotIn("disabled_relation_populated", checks.WAIVABLE_CODES)

    def test_disabled_stratum_blocks_stale_candidate(self):
        cand = self.build()
        fixtures.set_stratum(self.conn, "solo:west", enabled=False)
        report = self.check(cand["candidate_id"])
        self.assertIn("disabled_stratum_populated",
                      {f["code"] for f in report["findings"]})

    def test_tampered_payload_is_detected(self):
        cand = self.build()
        payload = json.loads(self.reload(cand["candidate_id"])["payload"])
        payload["rows"]["accounts"].append(
            {"account_id": 999999, "display_name": "Ghost", "segment": "solo",
             "region": "north", "status": "active"})
        self.conn.execute("UPDATE candidates SET payload=? WHERE candidate_id=?",
                          (canonical_json(payload), cand["candidate_id"]))
        self.conn.execute("COMMIT") if self.conn.in_transaction else None
        report = self.check(cand["candidate_id"])
        codes = {f["code"] for f in report["findings"]}
        self.assertTrue({"member_row_mismatch", "row_without_member"} & codes,
                        codes)

    def test_deleted_source_row_blocks_candidate(self):
        cand = self.build()
        victim = cand["payload"]["members"][2]["account_id"]
        self.conn.execute("DELETE FROM contacts WHERE account_id=?", (victim,))
        report = self.check(cand["candidate_id"])
        self.assertIn("source_row_missing",
                      {f["code"] for f in report["findings"]})

    def test_older_candidate_in_same_run_is_superseded(self):
        cand = self.build(label="same")
        core.build_candidate(self.conn, cand["run_id"])
        report = self.check(cand["candidate_id"])
        self.assertIn("snapshot_superseded",
                      {f["code"] for f in report["findings"]})
        self.assertNotIn("snapshot_superseded", checks.WAIVABLE_CODES)

    def test_integrity_findings_cannot_be_waived(self):
        cand = self.build()
        fixtures.withdraw_account(self.conn,
                                  cand["payload"]["members"][0]["account_id"])
        with self.assertRaises(ValueError):
            self.check(cand["candidate_id"], waivers=[
                {"code": "withdrawn_row_present", "note": "ignore it"}])

    def test_floor_conflict_can_be_waived_with_note(self):
        cand = self.build()
        thin = [m["account_id"] for m in cand["payload"]["members"]
                if m["stratum_id"] == "guild:west"]
        fixtures.withdraw_account(self.conn, thin[0])
        top = self.build(mode="topup", parent=cand["run_id"], label="top")
        report = self.check(top["candidate_id"], waivers=[
            {"code": "stratum_floor_unmet", "note": "population exhausted"}])
        self.assertEqual(report["verdict"], "pass")
        self.assertTrue(report["waived"])

    def test_weaker_waiver_needs_a_note(self):
        cand = self.build()
        thin = [m["account_id"] for m in cand["payload"]["members"]
                if m["stratum_id"] == "guild:west"]
        fixtures.withdraw_account(self.conn, thin[0])
        top = self.build(mode="topup", parent=cand["run_id"], label="top")
        with self.assertRaises(ValueError):
            self.check(top["candidate_id"],
                       waivers=[{"code": "stratum_floor_unmet", "note": ""}])


class PublisherTests(Base):
    def publish(self, cand, secret="k"):
        from clusterproof.publisher import core as pub
        return pub.publish(self.conn, cand["candidate_id"], secret)

    def test_release_verify_covers_empty_tables(self):
        from clusterproof.publisher import core as pub
        fixtures.set_relation(self.conn, "contacts", enabled=False)
        cand = self.build()
        out = self.publish(cand)
        self.assertEqual(out["row_counts"]["contacts"], 0)
        verify = pub.verify_release(self.conn, out["release_id"], "k")
        self.assertTrue(verify["signature_valid"])
        self.assertTrue(verify["row_counts_match"], verify["row_counts"])
        self.assertEqual(verify["leaked_withdrawals"], [])

    def test_withdrawn_row_cannot_be_republished(self):
        from clusterproof.publisher import core as pub
        cand = self.build()
        fixtures.withdraw_account(self.conn,
                                  cand["payload"]["members"][0]["account_id"])
        with self.assertRaises(pub.PublishError):
            self.publish(cand)

    def test_same_candidate_cannot_publish_twice(self):
        from clusterproof.publisher import core as pub
        cand = self.build()
        self.publish(cand)
        with self.assertRaises(pub.PublishError):
            self.publish(cand)

    def test_release_records_known_withdrawals(self):
        from clusterproof.publisher import core as pub
        cand = self.build()
        member_ids = {m["account_id"] for m in cand["payload"]["members"]}
        outsider = next(a for a in self.conn.execute(
            "SELECT account_id FROM accounts") if a[0] not in member_ids)[0]
        fixtures.withdraw_account(self.conn, outsider)  # never in the sample
        out = self.publish(cand)
        verify = pub.verify_release(self.conn, out["release_id"], "k")
        self.assertEqual(verify["leaked_withdrawals"], [])
        self.assertIn(f"account:{outsider}",
                      {w["target"] for w in
                       out["manifest"]["withdrawals_known_at_publish"]})


class FrameTests(Base):
    def test_allocation_respects_floor_and_population(self):
        config = core.load_config(self.conn)
        frame = core.build_frame(config, core.load_source(self.conn), "x")
        alloc = core.allocate(config, frame, 60)
        self.assertEqual(alloc["allocated"], 60)
        for entry in alloc["allocations"]:
            self.assertLessEqual(entry["quota"], entry["population"])
            self.assertGreaterEqual(entry["quota"], min(entry["min_rows"],
                                                        entry["population"]))

    def test_target_below_floor_rejected(self):
        config = core.load_config(self.conn)
        frame = core.build_frame(config, core.load_source(self.conn), "x")
        with self.assertRaises(core.SamplingError):
            core.allocate(config, frame, 5)

    def test_target_above_population_shrinks(self):
        config = core.load_config(self.conn)
        frame = core.build_frame(config, core.load_source(self.conn), "x")
        alloc = core.allocate(config, frame, 10000)
        self.assertLess(alloc["allocated"], 10000)


if __name__ == "__main__":
    unittest.main()
