"""Sampling engine.

Selection is deterministic: an account's position in a stratum is decided by
SHA-256(seed | stratum | account_id).  Re-running a run with an unchanged
source therefore reproduces the exact same sample, while newly inserted rows
slot into the same ordering instead of reshuffling everyone.

The engine never silently backfills.  If a stratum loses rows (withdrawal,
disabled stratum, exhausted frame) the shortfall is recorded and the sample
gets smaller; a row from another stratum is never borrowed to hit the target
count.
"""

from __future__ import annotations

import json
from collections import defaultdict

from ..db import tx
from ..util import canonical_json, sha256_hex, short_hash, utcnow

TABLE_COLUMNS = {
    "accounts": ("account_id", "display_name", "segment", "region", "status"),
    "orders": ("order_id", "account_id", "total_cents", "placed_on", "state"),
    "order_lines": ("line_id", "order_id", "sku", "qty", "unit_cents"),
    "contacts": ("contact_id", "account_id", "channel", "value"),
    "notes": ("note_id", "account_id", "body", "created_on"),
}
KEY_COLUMN = {"accounts": "account_id", "orders": "order_id",
              "order_lines": "line_id", "contacts": "contact_id", "notes": "note_id"}
SOURCE_WITHDRAWAL = {
    "accounts": "status = 'withdrawn'",
    "orders": "withdrawn_at IS NOT NULL",
    "order_lines": "withdrawn_at IS NOT NULL",
    "contacts": "withdrawn_at IS NOT NULL",
    "notes": "withdrawn_at IS NOT NULL",
}
SELECTOR_VERSION = "ranked-hash-v1"


class SamplingError(ValueError):
    """Design cannot be honoured at all (e.g. target below stratum floors)."""


# --------------------------------------------------------------------- config
def load_config(conn) -> dict:
    relations = [dict(r) for r in conn.execute(
        "SELECT child_table, parent_table, parent_col, required, enabled"
        " FROM relations ORDER BY child_table")]
    strata = [dict(r) for r in conn.execute(
        "SELECT stratum_id, segment, region, min_rows, enabled"
        " FROM strata ORDER BY stratum_id")]
    return {"relations": relations, "strata": strata}


def config_fingerprint(config: dict) -> str:
    return sha256_hex(canonical_json(config))


# --------------------------------------------------------------------- source
def load_source(conn) -> dict:
    source = {}
    for table, cols in TABLE_COLUMNS.items():
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        source[table] = {r[KEY_COLUMN[table]]: dict(r) for r in rows}
    withdrawn = {}
    for table, predicate in SOURCE_WITHDRAWAL.items():
        col = KEY_COLUMN[table]
        withdrawn[table] = {
            r[0] for r in conn.execute(f"SELECT {col} FROM {table} WHERE {predicate}")
        }
    return {"rows": source, "withdrawn": withdrawn}


def _rank(seed: str, stratum_id: str, account_id: int) -> str:
    return sha256_hex(f"{seed}|{stratum_id}|{account_id}")


def _bundle(account_id: int, config: dict, source: dict) -> dict:
    """Resolve one account and its enabled dependent rows.

    Child rows are followed transitively (accounts -> orders -> order_lines).
    Withdrawn rows and rows whose parent is not included are excluded and
    reported.  A member is unusable when a required relation ends up empty.
    """
    rows_by_table = source["rows"]
    withdrawn = source["withdrawn"]
    rows = {table: [] for table in TABLE_COLUMNS}
    omitted = []
    problems = []

    if account_id in withdrawn["accounts"]:
        return {"rows": rows,
                "omitted": [{"table": "accounts", "row_key": account_id,
                             "reason": "withdrawn"}],
                "problems": [{"code": "account_withdrawn",
                              "detail": {"account_id": account_id}}]}

    enabled = {r["child_table"]: r for r in config["relations"] if r["enabled"]}
    children_by_parent = defaultdict(list)
    for child_table, rel in enabled.items():
        for key, row in rows_by_table[child_table].items():
            children_by_parent[(child_table, rel["parent_col"], row[rel["parent_col"]])].append(key)
    for keys in children_by_parent.values():
        keys.sort()

    included = {table: set() for table in TABLE_COLUMNS}
    included["accounts"].add(account_id)
    rows["accounts"].append(rows_by_table["accounts"][account_id])

    queue = [("accounts", account_id)]
    while queue:
        parent_table, parent_key = queue.pop(0)
        for child_table, rel in sorted(enabled.items()):
            if rel["parent_table"] != parent_table:
                continue
            for key in children_by_parent.get((child_table, rel["parent_col"], parent_key), []):
                if key in included[child_table]:
                    continue
                included[child_table].add(key)
                if key in withdrawn[child_table]:
                    omitted.append({"table": child_table, "row_key": key,
                                    "account_id": account_id,
                                    "reason": "withdrawn"})
                    continue
                rows[child_table].append(rows_by_table[child_table][key])
                queue.append((child_table, key))

    # Child rows withdrawn from a member that otherwise survives: the member
    # stays (its account row is intact) but the page must say what was lost.
    for table in TABLE_COLUMNS:
        if table == "accounts":
            continue
        accounted = {o["row_key"] for o in omitted if o["table"] == table
                     and o["account_id"] == account_id}
        surviving_ids = {r[KEY_COLUMN[table]] for r in rows[table]}
        if table == "order_lines":
            parent_keys = {r["order_id"] for r in rows["orders"]}
            candidate_keys = {k for k, r in rows_by_table[table].items()
                              if r["order_id"] in parent_keys}
        else:
            candidate_keys = {k for k, r in rows_by_table[table].items()
                              if r.get("account_id") == account_id}
        for key in sorted(candidate_keys - surviving_ids - accounted,
                          key=lambda k: str(k)):
            if key in withdrawn[table]:
                omitted.append({"table": table, "row_key": key,
                                "account_id": account_id,
                                "reason": "withdrawn"})

    # Every required relation must contribute at least one row to the bundle.
    for rel in sorted(config["relations"], key=lambda r: r["child_table"]):
        if not rel["enabled"] or not rel["required"]:
            continue
        if not rows[rel["child_table"]]:
            problems.append({
                "code": "required_dependency_missing",
                "detail": {"relation": rel["child_table"],
                           "message": f"no usable {rel['child_table']} rows remain"},
            })

    for table in TABLE_COLUMNS:
        rows[table].sort(key=lambda r: r[KEY_COLUMN[table]])
    return {"rows": rows, "omitted": omitted, "problems": problems}


def build_frame(config: dict, source: dict, seed: str) -> dict:
    """Eligible account frame per enabled stratum, ranked by the run seed."""
    strata = {s["stratum_id"]: s for s in config["strata"] if s["enabled"]}
    frame = {sid: [] for sid in strata}
    ineligible = []
    for account_id, account in sorted(source["rows"]["accounts"].items()):
        sid = f"{account['segment']}:{account['region']}"
        if sid not in strata:
            continue
        if account_id in source["withdrawn"]["accounts"]:
            continue
        bundle = _bundle(account_id, config, source)
        if bundle["problems"]:
            ineligible.append({"account_id": account_id, "stratum_id": sid,
                               "problems": bundle["problems"]})
            continue
        frame[sid].append({
            "account_id": account_id,
            "stratum_id": sid,
            "rank": _rank(seed, sid, account_id),
            "bundle": bundle,
        })
    for sid in frame:
        frame[sid].sort(key=lambda m: (m["rank"], m["account_id"]))
    return {"frame": frame, "ineligible": ineligible, "strata": strata}


def allocate(config: dict, frame: dict, target: int) -> dict:
    """Largest-remainder allocation with min_rows floors and population caps."""
    strata = [s for s in config["strata"] if s["enabled"]]
    if not strata:
        raise SamplingError("no enabled strata")
    pop = {s["stratum_id"]: len(frame["frame"].get(s["stratum_id"], []))
           for s in strata}
    floor = {s["stratum_id"]: min(s["min_rows"], pop[s["stratum_id"]])
             for s in strata}
    total_floor = sum(floor.values())
    total_pop = sum(pop.values())
    if target < total_floor:
        raise SamplingError(
            f"target {target} is below the stratum floors ({total_floor}); "
            "lower min_rows or raise the target")
    quotas = dict(floor)

    # Floors can already exceed a proportional share; distribute the rest in
    # proportion to the remaining population.
    remaining = target - total_floor
    while remaining > 0:
        open_sids = [s["stratum_id"] for s in strata
                     if quotas[s["stratum_id"]] < pop[s["stratum_id"]]]
        if not open_sids:
            break
        open_pop = sum(pop[sid] for sid in open_sids)
        exact = {sid: remaining * pop[sid] / open_pop for sid in open_sids}
        granted = 0
        for sid in open_sids:
            add = min(int(exact[sid]), pop[sid] - quotas[sid])
            quotas[sid] += add
            granted += add
        remaining -= granted
        if remaining <= 0:
            break
        # Largest remainder, then stable stratum-id tie break.
        order = sorted(open_sids, key=lambda sid: (-(exact[sid] - int(exact[sid])), sid))
        for sid in order:
            if remaining <= 0:
                break
            if quotas[sid] < pop[sid]:
                quotas[sid] += 1
                remaining -= 1
                granted += 1
        if granted == 0:
            break

    allocations = [{
        "stratum_id": s["stratum_id"],
        "segment": s["segment"],
        "region": s["region"],
        "min_rows": s["min_rows"],
        "population": pop[s["stratum_id"]],
        "floor": floor[s["stratum_id"]],
        "quota": quotas[s["stratum_id"]],
    } for s in strata]
    return {"allocations": allocations, "target": target,
            "allocated": sum(quotas.values()),
            "unallocated": target - sum(quotas.values()),
            "population": total_pop}


# ------------------------------------------------------------------ selection
def _materialise(selected: dict, alloc: dict) -> tuple:
    rows = {table: [] for table in TABLE_COLUMNS}
    members = []
    omitted = []
    for entry in alloc["allocations"]:
        sid = entry["stratum_id"]
        chosen = selected.get(sid, [])
        entry["selected"] = len(chosen)
        for m in chosen:
            for table in TABLE_COLUMNS:
                rows[table].extend(dict(r) for r in m["bundle"]["rows"][table])
            omitted.extend(m["bundle"]["omitted"])
            members.append({
                "account_id": m["account_id"],
                "stratum_id": sid,
                "rank": m["rank"],
                "weight": round(entry["population"] / max(1, len(chosen)), 6),
                "dependencies": {t: len(m["bundle"]["rows"][t])
                                 for t in TABLE_COLUMNS if t != "accounts"},
            })
    for table in TABLE_COLUMNS:
        # Deduplicate: a required child row can only belong to one member, but
        # this guards against future many-to-many relations.
        seen = set()
        unique = []
        for row in rows[table]:
            key = row[KEY_COLUMN[table]]
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        unique.sort(key=lambda r: r[KEY_COLUMN[table]])
        rows[table] = unique
    members.sort(key=lambda m: (m["stratum_id"], m["rank"], m["account_id"]))
    return members, rows, omitted


def _weight_report(alloc: dict, selected: dict, previous: dict | None) -> dict:
    prev_by_sid = {}
    if previous:
        for entry in previous.get("design", {}).get("allocation", []):
            prev_by_sid[entry["stratum_id"]] = {
                "quota": entry.get("quota"),
                "population": entry.get("population"),
                "selected": entry.get("selected"),
            }
    strata = []
    for entry in alloc["allocations"]:
        sid = entry["stratum_id"]
        chosen = selected.get(sid, [])
        weight = entry["population"] / len(chosen) if chosen else None
        prev = prev_by_sid.get(sid)
        prev_weight = None
        prev_quota = None
        prev_population = None
        if prev is not None:
            prev_quota = prev.get("quota")
            prev_population = prev.get("population")
            prev_selected = prev.get("selected")
            if prev_selected:
                prev_weight = prev["population"] / prev_selected
        strata.append({
            "stratum_id": sid,
            "population": entry["population"],
            "previous_population": prev_population,
            "quota": entry["quota"],
            "previous_quota": prev_quota,
            "selected": len(chosen),
            "previous_selected": (prev or {}).get("selected"),
            "weight": round(weight, 6) if weight is not None else None,
            "previous_weight": round(prev_weight, 6) if prev_weight is not None else None,
            "weight_delta": (round(weight - prev_weight, 6)
                             if weight is not None and prev_weight is not None else None),
            "quota_change": (entry["quota"] - prev_quota
                             if prev_quota is not None else None),
        })
    return {"strata": strata}


def allocate_frozen(config: dict, frame: dict, parent_payload: dict,
                    requested_target: int) -> dict:
    """Top-up allocation: keep the parent's quotas, only shrink where the
    live frame cannot supply them.  Never move a freed slot to another
    stratum — that is exactly the silent padding this system refuses.
    """
    parent_alloc = parent_payload.get("design", {}).get("allocation", [])
    parent_by_sid = {a["stratum_id"]: a for a in parent_alloc}
    parent_target = parent_payload.get("design", {}).get("target_size")
    if requested_target is not None and parent_target is not None \
            and int(requested_target) != int(parent_target):
        raise SamplingError(
            f"top-up must keep the parent target ({parent_target}); got "
            f"{requested_target}. Start a new initial run to resize the sample.")
    allocations = []
    for stratum in sorted(config["strata"], key=lambda s: s["stratum_id"]):
        sid = stratum["stratum_id"]
        pop = len(frame["frame"].get(sid, [])) if stratum["enabled"] else 0
        parent = parent_by_sid.get(sid, {})
        # Keep the parent's declared quota even when the live frame is too
        # thin: the gap then appears as a declared shortfall instead of the
        # target quietly resizing itself.
        quota = int(parent.get("quota") or 0) if stratum["enabled"] else 0
        allocations.append({
            "stratum_id": sid,
            "segment": stratum["segment"],
            "region": stratum["region"],
            "min_rows": stratum["min_rows"],
            "population": pop,
            "floor": min(stratum["min_rows"], pop),
            "quota": quota,
            "selectable": min(quota, pop),
            "parent_quota": int(parent.get("quota") or 0),
            "parent_population": parent.get("population"),
            "parent_selected": parent.get("selected"),
            "enabled": bool(stratum["enabled"]),
        })
    target = sum(a["quota"] for a in allocations)
    allocated = sum(min(a["quota"], a["population"]) for a in allocations)
    return {"allocations": allocations, "target": target,
            "allocated": allocated, "unallocated": target - allocated,
            "population": sum(a["population"] for a in allocations),
            "frozen_from_parent": True, "parent_target": parent_target}


def _design_diagnostics(alloc: dict, frame: dict, selected: dict,
                        previously_selected: set | None = None) -> tuple:
    """Shortfalls/conflicts shared by initial and top-up builds."""
    shortfalls = []
    conflicts = []
    # `unallocated` counts design slots no stratum can fill.  Per-stratum
    # entries below explain each of those; only report a global gap if some
    # remains unattributed.
    capacity_gap = sum(max(0, e["quota"] - e["population"])
                       for e in alloc["allocations"])
    if alloc["unallocated"] - capacity_gap > 0:
        shortfalls.append({
            "stratum_id": None,
            "quota": alloc["target"],
            "selected": alloc["allocated"],
            "min_rows": None,
            "reason": "frame_exhausted",
            "message": (f"target {alloc['target']} exceeds the eligible frame "
                        f"({alloc['allocated']}); sample is smaller rather than "
                        "borrowing rows from another stratum"),
        })
    for entry in alloc["allocations"]:
        sid = entry["stratum_id"]
        chosen = selected.get(sid, [])
        missing = entry["quota"] - len(chosen)
        if missing > 0:
            shortfalls.append({
                "stratum_id": sid,
                "quota": entry["quota"],
                "selected": len(chosen),
                "min_rows": entry["min_rows"],
                "reason": "frame_exhausted",
                "message": (f"only {len(chosen)} eligible account(s) for quota "
                            f"{entry['quota']}; no rows were borrowed from another "
                            "stratum"),
            })
        if len(chosen) < entry["min_rows"]:
            conflicts.append({
                "code": "stratum_floor_unmet",
                "stratum_id": sid,
                "message": (f"{sid} needs {entry['min_rows']} member(s) but only "
                            f"{len(chosen)} eligible row(s) exist; sample is short "
                            "rather than padded with foreign rows"),
            })
        fully_included = (entry["population"] <= entry["min_rows"]
                          and entry["quota"] >= entry["population"])
        if fully_included and (previously_selected is None
                               or sid in previously_selected):
            conflicts.append({
                "code": "thin_stratum_fully_included",
                "stratum_id": sid,
                "message": (f"{sid} population ({entry['population']}) is at or "
                            f"below its floor ({entry['min_rows']}), and its "
                            f"quota ({entry['quota']}) takes all of it; any "
                            "withdrawal leaves no in-stratum replacement"),
            })
    return shortfalls, conflicts


def _select_initial(run: dict, config: dict, source: dict, target: int) -> dict:
    built = build_frame(config, source, run["seed"])
    alloc = allocate(config, built, target)
    selected = {}
    for entry in alloc["allocations"]:
        sid = entry["stratum_id"]
        selected[sid] = built["frame"].get(sid, [])[:entry["quota"]]

    members, rows, omitted = _materialise(selected, alloc)
    shortfalls, conflicts = _design_diagnostics(alloc, built, selected)
    for member in members:
        member["kept_from_parent"] = False
    ledger = {
        "kept": [],
        "added": [{"account_id": m["account_id"], "stratum_id": m["stratum_id"],
                   "reason": "initial_fill", "replaces": None,
                   "rank": m["rank"]} for m in members],
        "removed": [],
        "rehomed": [],
    }
    weights = _weight_report(alloc, selected, previous=None)
    return _payload(run, config, alloc, members, rows, ledger, weights,
                    shortfalls, conflicts, omitted, built["ineligible"])


def _payload(run, config, alloc, members, rows, ledger, weights,
             shortfalls, conflicts, omitted, frame_ineligible) -> dict:
    return {
        "config": config,
        "design": {
            "target_size": alloc["target"],
            "allocated": alloc["allocated"],
            "unallocated": alloc["unallocated"],
            "backfill_policy": "never",
            "allocation": alloc["allocations"],
        },
        "members": members,
        "ledger": ledger,
        "weights": weights,
        "shortfalls": shortfalls,
        "conflicts": conflicts,
        "rows": rows,
        "omitted": omitted,
        "frame_ineligible": frame_ineligible,
    }


def _latest_parent_candidate(conn, parent_run_id: str) -> dict:
    row = conn.execute(
        "SELECT candidate_id, payload, built_at, change_cursor FROM candidates"
        " WHERE run_id=? ORDER BY built_at DESC, candidate_id DESC LIMIT 1",
        (parent_run_id,)).fetchone()
    if row is None:
        raise SamplingError(f"parent run {parent_run_id} has no candidate to top up")
    return {"candidate_id": row["candidate_id"],
            "payload": json.loads(row["payload"]),
            "built_at": row["built_at"],
            "change_cursor": row["change_cursor"]}


def change_cursor(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(change_id), 0) AS c"
                       " FROM source_changes").fetchone()
    return int(row["c"])


def _new_inserted_accounts(conn, parent_cursor: int) -> set[int]:
    ids: set[int] = set()
    for row in conn.execute(
            "SELECT detail FROM source_changes WHERE kind='insert'"
            " AND change_id > ? ORDER BY change_id", (parent_cursor,)):
        ids.update(json.loads(row["detail"]).get("account_ids", []))
    return ids


def _select_topup(run: dict, config: dict, source: dict, target: int,
                  parent_payload: dict, inserted: set[int]) -> dict:
    built = build_frame(config, source, run["seed"])
    alloc = allocate_frozen(config, built, parent_payload, target)
    alloc_by_sid = {e["stratum_id"]: e for e in alloc["allocations"]}
    active_sids = {sid for sid, e in alloc_by_sid.items() if e["enabled"]}
    parent_members = {m["account_id"]: m for m in parent_payload.get("members", [])}
    eligible_by_id = {m["account_id"]: m
                      for entries in built["frame"].values() for m in entries}

    kept_by_sid = {sid: [] for sid in active_sids}
    removed, rehomed = [], []
    for account_id, member in sorted(parent_members.items()):
        sid = member["stratum_id"]
        current = eligible_by_id.get(account_id)
        if sid not in active_sids:
            reason = "stratum_disabled"
            if current is not None and current["stratum_id"] in active_sids:
                reason = "moved_into_disabled_stratum"
            removed.append({
                "account_id": account_id, "stratum_id": sid, "reason": reason,
                "detail": {"now": current["stratum_id"] if current else None,
                           "was": sid},
                "replaced_by": None,
            })
            continue
        if current is None:
            reason = "account_withdrawn"
            account = source["rows"]["accounts"].get(account_id)
            if account and account_id not in source["withdrawn"]["accounts"]:
                reason = "required_dependency_withdrawn"
            removed.append({
                "account_id": account_id, "stratum_id": sid, "reason": reason,
                "detail": {"parent_rank": member.get("rank")},
                "replaced_by": None,
            })
            continue
        new_sid = current["stratum_id"]
        if new_sid != sid:
            rehomed.append({"account_id": account_id, "from": sid, "to": new_sid})
        current["rank"] = member.get("rank", current["rank"])
        current["moved_from"] = sid
        kept_by_sid[new_sid].append(current)

    selected = {}
    additions = []
    for entry in alloc["allocations"]:
        sid = entry["stratum_id"]
        if not entry["enabled"]:
            selected[sid] = []
            continue
        kept = sorted(kept_by_sid[sid], key=lambda m: (m["rank"], m["account_id"]))
        quota = entry["quota"]
        if len(kept) > quota:
            for m in kept[quota:]:
                moved = m.get("moved_from") and m["moved_from"] != sid
                removed.append({
                    "account_id": m["account_id"], "stratum_id": sid,
                    "reason": "moved_into_full_stratum" if moved else "stratum_shrank",
                    "detail": {"quota": quota,
                               "moved_from": m.get("moved_from") or sid},
                    "replaced_by": None,
                })
            kept = kept[:quota]
        selected[sid] = list(kept)
        remaining = quota - len(kept)
        if remaining <= 0:
            continue
        kept_ids = {m["account_id"] for m in kept}
        pool = [m for m in built["frame"].get(sid, [])
                if m["account_id"] not in kept_ids
                and m["account_id"] not in parent_members]
        # Newly arrived rows first (that is what a top-up is for), then stable
        # ranked-hash order among the rest.
        pool.sort(key=lambda m: (
            0 if m["account_id"] in inserted else 1, m["rank"], m["account_id"]))
        added = pool[:remaining]
        for m in added:
            selected[sid].append(m)
            additions.append({
                "account_id": m["account_id"], "stratum_id": sid,
                "reason": "new_data" if m["account_id"] in inserted else "replacement",
                "replaces": None, "rank": m["rank"],
            })

    # Pair each vacancy with a same-stratum addition so the page can state
    # exactly what replaced what; never pair across strata.
    adds_by_sid = defaultdict(list)
    for add in additions:
        adds_by_sid[add["stratum_id"]].append(add)
    for rem in removed:
        pool = adds_by_sid.get(rem["stratum_id"], [])
        if pool and rem["reason"] not in ("stratum_shrank", "stratum_disabled"):
            replacement = pool.pop(0)
            rem["replaced_by"] = replacement["account_id"]
            replacement["replaces"] = rem["account_id"]
        else:
            rem["replaced_by"] = None

    members, rows, omitted = _materialise(selected, alloc)
    for member in members:
        parent = parent_members.get(member["account_id"])
        member["kept_from_parent"] = parent is not None
        member["weight"] = _member_weight(alloc_by_sid[member["stratum_id"]],
                                          selected[member["stratum_id"]])
    members.sort(key=lambda m: (m["stratum_id"], 0 if m["kept_from_parent"] else 1,
                                m["rank"], m["account_id"]))
    parent_sids = {m["stratum_id"] for m in parent_members.values()}
    shortfalls, conflicts = _design_diagnostics(alloc, built, selected,
                                                previously_selected=parent_sids)
    if removed:
        conflicts.append({
            "code": "member_stability_tradeoff",
            "stratum_id": None,
            "message": (f"{len(removed)} member(s) left the sample; replacements "
                        "were only taken inside the same stratum and no count was "
                        "silently padded"),
        })
    ledger = {
        "kept": sorted(m["account_id"] for m in members if m["kept_from_parent"]),
        "added": additions,
        "removed": removed,
        "rehomed": rehomed,
    }
    weights = _weight_report(alloc, selected, previous=parent_payload)
    payload = _payload(run, config, alloc, members, rows, ledger, weights,
                       shortfalls, conflicts, omitted, built["ineligible"])
    payload["design"]["parent_target"] = parent_payload.get("design", {}).get("target_size")
    payload["parent"] = {
        "run_id": parent_payload.get("run", {}).get("run_id"),
        "candidate_id": None,
        "payload_hash": sha256_hex(canonical_json(parent_payload)),
    }
    return payload


def _member_weight(entry: dict, chosen: list) -> float:
    return round(entry["population"] / max(1, len(chosen)), 6)


# ------------------------------------------------------------------- run/build
def create_run(conn, label: str, seed, sample_size: int, mode: str = "initial",
               parent_run_id: str | None = None, salt: str | None = None) -> dict:
    if mode not in ("initial", "topup"):
        raise SamplingError("mode must be initial or topup")
    if mode == "topup" and not parent_run_id:
        raise SamplingError("topup runs need parent_run_id")
    try:
        size = int(sample_size)
    except (TypeError, ValueError):
        raise SamplingError("sample_size must be an integer")
    if size <= 0:
        raise SamplingError("sample_size must be positive")
    seed = str(seed)
    if not seed:
        raise SamplingError("seed must be non-empty")
    if parent_run_id and conn.execute("SELECT 1 FROM runs WHERE run_id=?",
                                      (parent_run_id,)).fetchone() is None:
        raise SamplingError(f"parent run {parent_run_id} not found")
    now = utcnow()
    base = "run-" + short_hash(label, seed, str(size), mode, parent_run_id or "", now)
    run_id = base
    attempt = 1
    with tx(conn):
        # Same label+seed+second is legitimate (e.g. parallel jobs); give each
        # run a distinct id instead of failing on the primary key.
        while conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
            attempt += 1
            run_id = f"{base}-{attempt:02d}"
        conn.execute(
            "INSERT INTO runs(run_id, label, seed, sample_size, salt, created_at,"
            " mode, parent_run_id, state, notes) VALUES (?,?,?,?,?,?,?,?,?,'')",
            (run_id, label, seed, size, salt or "", now, mode, parent_run_id, "building"),
        )
    return get_run(conn, run_id)


def get_run(conn, run_id: str) -> dict:
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"run {run_id} not found")
    data = dict(row)
    data["candidates"] = [dict(r) for r in conn.execute(
        "SELECT candidate_id, built_at, payload_hash, member_count, row_count,"
        " selector_version, state FROM candidates WHERE run_id=? ORDER BY built_at",
        (run_id,))]
    return data


def build_candidate(conn, run_id: str) -> dict:
    run = get_run(conn, run_id)
    config = load_config(conn)
    source = load_source(conn)
    if run["mode"] == "topup":
        parent = _latest_parent_candidate(conn, run["parent_run_id"])
        inserted = _new_inserted_accounts(conn, parent["change_cursor"])
        payload = _select_topup(run, config, source, run["sample_size"],
                                parent["payload"], inserted)
        payload["parent"]["candidate_id"] = parent["candidate_id"]
        payload["parent"]["run_id"] = payload["parent"].get("run_id") or run["parent_run_id"]
    else:
        payload = _select_initial(run, config, source, run["sample_size"])

    payload["run"] = {
        "run_id": run["run_id"], "label": run["label"], "seed": run["seed"],
        "salt": run["salt"], "target_size": run["sample_size"],
        "mode": run["mode"], "parent_run_id": run["parent_run_id"],
    }
    payload["selector"] = {"version": SELECTOR_VERSION}
    payload["config_fingerprint"] = config_fingerprint(config)

    row_count = sum(len(v) for v in payload["rows"].values())
    payload_hash = sha256_hex(canonical_json(payload))
    existing = conn.execute("SELECT COUNT(*) FROM candidates WHERE run_id=?",
                            (run_id,)).fetchone()[0]
    candidate_id = f"{run_id}-c{existing + 1:02d}"
    now = utcnow()
    cursor = change_cursor(conn)
    with tx(conn):
        built_at = now
        conn.execute(
            "INSERT INTO candidates(candidate_id, run_id, built_at, payload,"
            " payload_hash, member_count, row_count, selector_version, state,"
            " observation, change_cursor) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, run_id, built_at, canonical_json(payload), payload_hash,
             len(payload["members"]), row_count, SELECTOR_VERSION, "draft",
             "{}", cursor),
        )
        conn.execute(
            "INSERT INTO candidate_events(candidate_id, at, kind, detail)"
            " VALUES (?,?,?,?)",
            (candidate_id, built_at, "built", canonical_json({
                "members": len(payload["members"]), "rows": row_count,
                "payload_hash": payload_hash,
                "removed": len(payload["ledger"]["removed"]),
                "added": len(payload["ledger"]["added"]),
                "kept": len(payload["ledger"]["kept"]),
            })),
        )
    return get_candidate(conn, candidate_id)


def get_candidate(conn, candidate_id: str) -> dict:
    row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?",
                       (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(f"candidate {candidate_id} not found")
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    data["events"] = [
        {**dict(e), "detail": json.loads(e["detail"])}
        for e in conn.execute(
            "SELECT at, kind, detail FROM candidate_events WHERE candidate_id=?"
            " ORDER BY event_id", (candidate_id,))
    ]
    return data


def record_check(conn, candidate_id: str, verdict: str, findings: list,
                 waivers: list | None = None) -> None:
    state = "checked" if verdict == "pass" else "failed"
    now = utcnow()
    with tx(conn):
        conn.execute("UPDATE candidates SET state=? WHERE candidate_id=?",
                     (state, candidate_id))
        conn.execute(
            "INSERT INTO candidate_events(candidate_id, at, kind, detail)"
            " VALUES (?,?,?,?)",
            (candidate_id, now, "check", canonical_json({
                "verdict": verdict, "findings": findings,
                "waivers": waivers or []})),
        )
