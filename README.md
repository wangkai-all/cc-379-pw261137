# ClusterProof

A runnable reference implementation of **continuous test-sample generation over
relational data**: pick a row from the main table, drag along the dependent rows
it needs, and keep the sample honest when a sensitive row is withdrawn.

Everything in this repository is fictional: invented accounts, invented names,
invented orders. There is no connector to any real system.

## What problem this solves

A test-data sampler that only bumps a row count when something is withdrawn is
lying to you. Three things fight each other:

1. **Membership stability** — people build assertions against account #1042; if
   the sampler reshuffles everyone whenever one row is erased, every downstream
   test breaks.
2. **Representativeness** — every stratum (segment × region) needs a minimum
   number of rows, or the "sample" quietly stops covering a cell.
3. **Sensitive-row withdrawal** — when a row is withdrawn, the whole dependent
   bundle must leave, and it must never come back — including through an old
   queued job.

ClusterProof refuses to resolve that by padding. When a stratum loses its last
eligible member, the sample gets **smaller** and says so: a declared shortfall,
a recorded weight change, and — only if an operator explicitly accepts it with a
written reason — a visibly waived representativeness conflict. Integrity
problems (withdrawn rows, broken links, disabled relations) can never be waived.

## Run it from an empty directory

```bash
python3 -m clusterproof.cli serve --db var/demo.sqlite3 --seed --reset
# console: http://127.0.0.1:8710
```

Requires Python 3.11+ and nothing else (standard library only). Four processes
start under one supervisor:

| process   | port | responsibility |
|-----------|------|----------------|
| console   | 8710 | UI + thin proxy; holds no database connection |
| sampler   | 8711 | reads source, builds runs/candidates, asks checker |
| checker   | 8712 | re-derives all invariants from source; read-only |
| publisher | 8713 | the only writer of releases; re-checks under lock |

They share one SQLite file in WAL mode, so you can stop and restart any of them
independently:

```bash
python3 -m clusterproof.sampler.service --db var/demo.sqlite3
python3 -m clusterproof.checker.service   --db var/demo.sqlite3
python3 -m clusterproof.publisher.service --db var/demo.sqlite3
```

## See the whole story

With the stack running:

```bash
python3 scripts/acceptance.py            # end-to-end narrative, 50 assertions
python3 scripts/race_test.py             # withdrawal racing publish
python3 -m unittest discover -s tests    # 33 engine tests
node scripts/ui_render_test.js /tmp/render_samples.json   # UI render smoke
```

`acceptance.py` drives, against a live stack:

1. initial sample from the empty database, then a same-seed replay proving the
   selection is reproducible;
2. publication of release 1;
3. interleaved source events while a job is queued — a relation is disabled, a
   thin-stratum account and an ordinary account are withdrawn, a stratum floor
   is re-raised, and new rows arrive;
4. the queued candidate is now **refused** at publish (its snapshot predates the
   withdrawals and the relation change);
5. a top-up run keeps the surviving members, replaces only inside the same
   stratum, uses newly arrived rows first, and reports the one real shortfall;
6. the operator accepts that conflict with a written reason and publishes
   release 2, which supersedes release 1 and carries the waiver in its signed
   manifest;
7. release 1 is still verifiable and still immutable — and now flagged as having
   post-publication withdrawals.

## How sampling works

**Deterministic rank.** An account's position inside its stratum is
`sha256(seed | stratum | account_id)`. The same seed against unchanged source
reproduces exactly the same members and rows; new rows slot into that ordering
rather than reshuffling existing members.

**Largest-remainder allocation** fills the target size proportionally to each
stratum's eligible population, after honouring each stratum's `min_rows` floor.
A quota can never exceed its population.

**Dependency closure.** Relations are declared in the `relations` table
(`child_table`, `parent_table`, `parent_col`, `required`, `enabled`) and followed
transitively: `accounts → orders → order_lines`, plus `contacts` and `notes`.
Withdrawn children are excluded; if a *required* relation ends up empty for a
member, that member is ineligible rather than half-loaded.

**Top-ups are frozen.** A top-up run inherits the parent's per-stratum quotas
and its target size. It keeps every parent member that is still eligible, in
stratum, and inside quota. Vacancies are filled only from the same stratum, with
newly arrived rows first. A vacancy the stratum cannot fill stays a vacancy —
the target is not quietly resized, and no row is borrowed from another cell.

## What the page shows, and why

- **Membership change ledger** — each removal carries its reason
  (`account_withdrawn`, `required_dependency_withdrawn`, `stratum_disabled`,
  `moved_into_full_stratum`, `stratum_shrank`) and, where one exists, the
  same-stratum replacement account. Nothing is renamed into "refreshed".
- **Weight table** — weight is population ÷ members actually included, with the
  previous weight and the delta. A drained stratum becomes heavier per remaining
  member; that is disclosed instead of being hidden by holding the count steady.
- **Shortfalls** — declared per stratum, with the target still shown alongside
  the smaller delivered size.
- **Checker findings** — errors, warnings and info separately; waived findings
  keep their original text plus the operator's note.
- **Release page** — signed manifest, signature verification, accepted
  conflicts, the churn recorded at freeze time, and whether any withdrawal known
  at publish appears in the frozen rows.

## Guarantees and where they are enforced

| guarantee | enforcement |
|---|---|
| withdrawn rows never enter a release | sampler excludes them; checker compares against live source; publisher re-checks inside its write transaction |
| an old queued job cannot re-emit erased data | candidate stores a `change_cursor`; the checker compares against source at check and at publish time |
| a disabled relation is not re-emitted | checker treats populated disabled relations as a blocking error, including for candidates built earlier |
| sample size is never padded | allocation caps at population; shortfall is a first-class payload field; checker fails `silent_size_drift` if members ≠ target − declared gap |
| same seed reproduces the same sample | ranked-hash selection; `acceptance.py` replays it |
| releases are immutable | release rows are copied once; later withdrawals are disclosed, never applied |
| waivers cannot hide data leakage | `WAIVABLE_CODES` contains only `stratum_floor_unmet`; every integrity code is rejected before the check runs |

## Deliberate limits

- SQLite-backed and single-host: it demonstrates the workflow, not a production
  scheduler. The three services share a file because that is enough to show
  isolation of responsibility.
- The rank hash is not keyed. For real data, key it with a secret so membership
  cannot be reconstructed from public identifiers.
- `--secret` for release signing defaults to a demo value; set
  `CLUSTERPROOF_SIGNING_KEY` for anything real.
- Fixture dates are pinned so the demo is reproducible; the system clock is only
  used for audit timestamps, never for selection.
