"""Independent constraint checker.

The checker re-derives everything from the source tables and the candidate
payload.  It does not trust the sampler's summary flags: it verifies actual
row membership, dependency closure, withdrawal status, stratum coverage and
design-key agreement with the current configuration.

Checks are grouped by severity:
  error   blocks publishing
  warning is shown on the release page but does not block
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from ..sampler import core
from ..util import utcnow

KEY_COLUMN = core.KEY_COLUMN
SEVERITY = {"error": 2, "warning": 1, "info": 0}

# Conflicts an operator may explicitly accept, with a recorded note, when the
# source genuinely cannot satisfy them.  Integrity violations are never
# waivable: a withdrawn row or a broken parent/child link must always block.
WAIVABLE_CODES = {
    "stratum_floor_unmet",
}
NON_WAIVABLE_CODES = {
    "withdrawn_row_present", "member_row_mismatch", "row_without_member",
    "orphan_child", "required_dependency_missing", "stratum_label_wrong",
    "silent_size_drift", "dangling_replacement", "duplicate_row",
    "ledger_contradiction", "unknown_stratum",
    "disabled_relation_populated", "disabled_stratum_populated",
    "source_row_missing", "snapshot_superseded",
}


def apply_waivers(report: dict, waivers: list[dict]) -> dict:
    """Mark acknowledged conflicts.  The report keeps every finding; waived
    errors stop blocking, but are stamped so they remain visible on the page.
    """
    if not waivers:
        return report
    by_code = {}
    for waiver in waivers:
        code = waiver.get("code")
        note = (waiver.get("note") or "").strip()
        if code not in WAIVABLE_CODES:
            raise ValueError(
                f"conflict {code!r} cannot be waived; only {sorted(WAIVABLE_CODES)} "
                "are operator-acceptable")
        if not note:
            raise ValueError(f"waiver for {code!r} needs a non-empty note")
        by_code.setdefault(code, []).append({"note": note,
                                            "at": waiver.get("at")})
    waived = []
    blocking = []
    for finding in report["findings"]:
        entry = None
        if by_code.get(finding["code"]):
            entry = by_code[finding["code"]].pop(0)
        if entry is not None and finding["severity"] == "error":
            finding["waived"] = entry
            waived.append({"code": finding["code"], **entry,
                           "detail": finding.get("detail", {})})
        elif finding["severity"] == "error":
            blocking.append(finding)
    report["waived"] = waived
    report["errors"] = len(blocking)
    report["verdict"] = "fail" if blocking else "pass"
    report["publishable"] = not blocking
    report["waiver_policy"] = sorted(WAIVABLE_CODES)
    return report


class Checker:
    def __init__(self, conn, candidate: dict, config: dict):
        self.conn = conn
        self.candidate = candidate
        self.payload = candidate["payload"]
        self.config = config
        self.findings: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def add(self, severity: str, code: str, message: str, **detail):
        self.findings.append({
            "severity": severity, "code": code, "message": message,
            "detail": detail,
        })

    def rows_of(self, table: str) -> list[dict]:
        return self.payload.get("rows", {}).get(table, [])

    def keys_of(self, table: str) -> set:
        col = KEY_COLUMN[table]
        return {r[col] for r in self.rows_of(table)}

    # ------------------------------------------------------------------- checks
    def check_member_rows(self):
        accounts = {r["account_id"]: r for r in self.rows_of("accounts")}
        members = {m["account_id"] for m in self.payload.get("members", [])}
        if set(accounts) != members:
            self.add("error", "member_row_mismatch",
                     "accounts rows and member list disagree",
                     only_rows=sorted(set(accounts) - members),
                     only_members=sorted(members - set(accounts)))
        for table in ("orders", "contacts", "notes"):
            stray = self.keys_of(table) - self._descendant_keys(table)
            if stray:
                self.add("error", "row_without_member",
                         f"{table} contains rows not reachable from a member",
                         rows=sorted(stray)[:20])
        line_orders = {r["order_id"] for r in self.rows_of("orders")}
        for line in self.rows_of("order_lines"):
            if line["order_id"] not in line_orders:
                self.add("error", "orphan_child",
                         "order_lines row references an order not in the sample",
                         line_id=line["line_id"], order_id=line["order_id"])

    def _descendant_keys(self, table: str) -> set:
        members = {m["account_id"] for m in self.payload.get("members", [])}
        if table == "orders":
            return {r["order_id"] for r in self.rows_of("orders")
                    if r["account_id"] in members}
        if table == "order_lines":
            order_ids = {r["order_id"] for r in self.rows_of("orders")
                         if r["account_id"] in members}
            return {r["line_id"] for r in self.rows_of("order_lines")
                    if r["order_id"] in order_ids}
        col = KEY_COLUMN[table]
        return {r[col] for r in self.rows_of(table) if r["account_id"] in members}

    def check_withdrawals(self):
        """No withdrawn source row may appear in the candidate.

        A row can be (a) still present in source and flagged withdrawn, or
        (b) hard-deleted from source after being sampled.  Both must block.
        """
        for table, col in KEY_COLUMN.items():
            predicate = core.SOURCE_WITHDRAWAL[table]
            rows = {r[0]: r[1] for r in self.conn.execute(
                f"SELECT {col}, {predicate} AS gone FROM {table}")}
            withdrawn = {k for k, gone in rows.items() if gone}
            deleted = self.keys_of(table) - set(rows)
            leaked = sorted((self.keys_of(table) & withdrawn) | deleted)
            if deleted:
                self.add("error", "source_row_missing",
                         f"{table} includes row(s) no longer present in source",
                         table=table, rows=sorted(deleted)[:20],
                         count=len(deleted))
            if leaked:
                self.add("error", "withdrawn_row_present",
                         f"{table} includes rows withdrawn at source",
                         table=table, rows=leaked[:20], count=len(leaked))

        # Also refuse a candidate whose snapshot predates a withdrawal: the
        # payload is a frozen copy, so compare against build time too.
        cursor = self.candidate.get("change_cursor") or 0
        changes = self.conn.execute(
            "SELECT at, kind, target FROM source_changes WHERE kind='withdrawal'"
            " AND change_id > ? ORDER BY change_id", (cursor,)).fetchall()
        if changes:
            self.add("warning", "source_changed_since_build",
                     f"{len(changes)} withdrawal(s) happened after this candidate "
                     "was built; rebuild before publishing",
                     changes=[dict(c) for c in changes][:20])

    def check_dependencies(self):
        """Every enabled+required relation must be complete for each member."""
        members = {m["account_id"] for m in self.payload.get("members", [])}
        orders = {r["order_id"]: r for r in self.rows_of("orders")}
        lines_by_order = Counter(r["order_id"] for r in self.rows_of("order_lines"))
        present = {
            "orders": Counter(r["account_id"] for r in self.rows_of("orders")),
            "contacts": Counter(r["account_id"] for r in self.rows_of("contacts")),
            "notes": Counter(r["account_id"] for r in self.rows_of("notes")),
        }
        for rel in sorted(self.config["relations"], key=lambda r: r["child_table"]):
            table = rel["child_table"]
            if not rel["enabled"]:
                if self.rows_of(table):
                    self.add("error", "disabled_relation_populated",
                             f"{table} is disabled in the live design but the "
                             "candidate still carries its rows; a queued job must "
                             "not re-emit a disabled dependency",
                             relation=table, rows=len(self.rows_of(table)))
                continue
            if not rel["required"]:
                continue
            if rel["parent_table"] == "accounts":
                missing = sorted(a for a in members if present[table][a] == 0)
                detail = {"relation": table, "missing_accounts": missing[:20]}
                why = f"{len(missing)} member(s) have no required {table} rows"
            else:
                missing_lines = sorted(
                    oid for oid, order in orders.items()
                    if order["account_id"] in members and lines_by_order[oid] == 0)
                missing = missing_lines
                detail = {"relation": table, "orders_without_rows": missing[:20]}
                why = f"{len(missing)} parent row(s) have no required {table} rows"
            if missing:
                self.add("error", "required_dependency_missing", why, **detail)

    def check_strata(self):
        enabled = [s for s in self.config["strata"] if s["enabled"]]
        by_sid = Counter(m["stratum_id"] for m in self.payload.get("members", []))
        member_by_id = {m["account_id"]: m for m in self.payload.get("members", [])}
        accounts = {r["account_id"]: r for r in self.rows_of("accounts")}
        for stratum in enabled:
            sid = stratum["stratum_id"]
            got = by_sid.get(sid, 0)
            # Cross-check the label against the account's actual segment/region.
            wrong = [a for a, m in member_by_id.items()
                     if m["stratum_id"] == sid
                     and f"{accounts[a]['segment']}:{accounts[a]['region']}" != sid]
            if wrong:
                self.add("error", "stratum_label_wrong",
                         f"{len(wrong)} member(s) labelled {sid} do not belong there",
                         stratum=sid, accounts=sorted(wrong)[:20])
            if got < stratum["min_rows"]:
                self.add("error", "stratum_floor_unmet",
                         f"{sid} has {got} member(s), below min_rows "
                         f"{stratum['min_rows']}",
                         stratum=sid, have=got, need=stratum["min_rows"])
            source_population = self.conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE segment=? AND region=?"
                " AND status='active'", (stratum["segment"], stratum["region"])
            ).fetchone()[0]
            if got == 0 and source_population > 0:
                self.add("warning", "stratum_empty",
                         f"{sid} has rows available but is not represented",
                         stratum=sid, population=source_population)
        known = {s["stratum_id"] for s in self.config["strata"]}
        enabled_ids = {s["stratum_id"] for s in self.config["strata"] if s["enabled"]}
        for sid in by_sid:
            if sid not in known:
                self.add("error", "unknown_stratum",
                         f"member labelled with unknown stratum {sid}", stratum=sid)
            elif sid not in enabled_ids:
                self.add("error", "disabled_stratum_populated",
                         f"stratum {sid} is disabled but the candidate still "
                         "emits members from it", stratum=sid, members=by_sid[sid])

    def check_design_agreement(self):
        # An older candidate in the same run is a stale snapshot: a later
        # build (or publish) supersedes it.  Re-emitting it would put older
        # source content back on the wire.
        # Build order is insertion order (rowid), not the one-second clock.
        later = self.conn.execute(
            "SELECT c.candidate_id FROM candidates c"
            " WHERE c.run_id=? AND c.rowid > (SELECT rowid FROM candidates"
            " WHERE candidate_id=?) LIMIT 1",
            (self.candidate["run_id"], self.candidate["candidate_id"])).fetchone()
        if later:
            self.add("error", "snapshot_superseded",
                     "a newer candidate exists in this run; rebuild instead of "
                     "re-emitting the older snapshot",
                     newer_candidate=later["candidate_id"])
        fingerprint = core.config_fingerprint(self.config)
        if self.payload.get("config_fingerprint") != fingerprint:
            self.add("warning", "design_changed_since_build",
                     "relation/stratum design changed after this candidate was "
                     "built; rebuild to re-evaluate against the new design")
        declared = self.payload.get("config", {})
        if declared != self.config:
            self.add("warning", "stale_design_embedded",
                     "candidate embeds a different design snapshot")

    def check_ledger(self):
        ledger = self.payload.get("ledger", {})
        members = {m["account_id"] for m in self.payload.get("members", [])}
        added = {a["account_id"] for a in ledger.get("added", [])}
        kept = set(ledger.get("kept", []))
        removed = {r["account_id"] for r in ledger.get("removed", [])}
        unaccounted = members - added - kept
        if unaccounted:
            self.add("warning", "ledger_incomplete",
                     f"{len(unaccounted)} member(s) have no ledger entry",
                     accounts=sorted(unaccounted)[:20])
        overlap = added & removed
        if overlap:
            self.add("error", "ledger_contradiction",
                     "same account is listed as both added and removed",
                     accounts=sorted(overlap)[:20])
        replaced = [r for r in ledger.get("removed", []) if r.get("replaced_by")]
        for rem in replaced:
            if rem["replaced_by"] not in members:
                self.add("error", "dangling_replacement",
                         "replacement account is not actually in the sample",
                         account=rem["account_id"],
                         replaced_by=rem["replaced_by"])
            if rem.get("stratum_id") != next(
                    (a["stratum_id"] for a in ledger.get("added", [])
                     if a["account_id"] == rem["replaced_by"]), rem.get("stratum_id")):
                self.add("warning", "cross_stratum_replacement",
                         "replacement came from a different stratum",
                         account=rem["account_id"])
        # No silent padding: total members must equal target minus declared
        # shortfall, and declared shortfalls must sum to the gap.
        design = self.payload.get("design", {})
        target = design.get("target_size")
        shortfalls = self.payload.get("shortfalls", [])
        declared_gap = sum(
            (s.get("quota") or 0) - (s.get("selected") or 0) for s in shortfalls
            if s.get("stratum_id"))
        total_gap = sum(s.get("unallocated") or 0 for s in [design])
        actual_gap = (target or len(members)) - len(members)
        if target is not None and actual_gap != total_gap:
            self.add("error", "silent_size_drift",
                     f"target {target} but {len(members)} members with "
                     f"{total_gap} declared unallocated",
                     target=target, members=len(members), declared=total_gap)
        if shortfalls and declared_gap and declared_gap < actual_gap:
            self.add("warning", "shortfall_underdeclared",
                     "sample is short but the per-stratum shortfall does not "
                     "explain the gap")

    def check_weights(self):
        members = self.payload.get("members", [])
        by_sid = defaultdict(list)
        for m in members:
            by_sid[m["stratum_id"]].append(m)
        allocation = {a["stratum_id"]: a for a in
                      self.payload.get("design", {}).get("allocation", [])}
        for sid, rows in by_sid.items():
            entry = allocation.get(sid)
            if not entry:
                continue
            expected = entry["population"] / len(rows)
            for m in rows:
                if abs((m.get("weight") or 0) - expected) > 1e-6:
                    self.add("warning", "weight_mismatch",
                             f"member weight {m.get('weight')} does not match "
                             f"{expected:.6f} for stratum {sid}",
                             account=m["account_id"], stratum=sid)
                    break
            if entry.get("population") != len(rows) and entry.get("selected") != len(rows):
                self.add("warning", "weight_population_stale",
                         f"stratum {sid} population/selected do not match member count",
                         stratum=sid, population=entry.get("population"),
                         selected=entry.get("selected"), actual=len(rows))

    def check_duplicates_and_freshness(self):
        seen = {}
        for table, col in KEY_COLUMN.items():
            for row in self.rows_of(table):
                key = (table, row[col])
                if key in seen:
                    self.add("error", "duplicate_row",
                             f"{table} row appears more than once", table=table,
                             row_key=row[col])
                seen[key] = True
        cursor = self.candidate.get("change_cursor") or 0
        pulled_at = self.conn.execute(
            "SELECT COUNT(*) FROM source_changes WHERE kind IN ('withdrawal')"
            " AND change_id > ?", (cursor,)).fetchone()[0]
        if pulled_at == 0:
            self.add("info", "snapshot_current",
                     "no withdrawals recorded since this candidate was built")

    def run(self, waivers: list[dict] | None = None) -> dict:
        self.check_member_rows()
        self.check_withdrawals()
        self.check_dependencies()
        self.check_strata()
        self.check_design_agreement()
        self.check_ledger()
        self.check_weights()
        self.check_duplicates_and_freshness()
        errors = [f for f in self.findings if f["severity"] == "error"]
        warnings = [f for f in self.findings if f["severity"] == "warning"]
        counts = Counter(f["code"] for f in self.findings)
        verdict = "fail" if errors else "pass"
        report = {
            "candidate_id": self.candidate["candidate_id"],
            "verdict": verdict,
            "publishable": not errors,
            "errors": len(errors),
            "warnings": len(warnings),
            "codes": dict(counts),
            "findings": sorted(self.findings,
                               key=lambda f: (-SEVERITY[f["severity"]], f["code"])),
            "waived": [],
            "waiver_policy": sorted(WAIVABLE_CODES),
            "checked_at": utcnow(),
        }
        if waivers:
            report = apply_waivers(report, waivers)
        return report


def check_candidate(conn, candidate_row: dict, config: dict | None = None,
                    waivers: list[dict] | None = None) -> dict:
    candidate = dict(candidate_row)
    if isinstance(candidate["payload"], str):
        candidate["payload"] = json.loads(candidate["payload"])
    if config is None:
        config = core.load_config(conn)
    return Checker(conn, candidate, config).run(waivers=waivers)
