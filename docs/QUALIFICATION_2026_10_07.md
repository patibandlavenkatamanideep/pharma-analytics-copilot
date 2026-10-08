# Release qualification pass — 7 October 2026

A targeted qualification of the candidate on `codex/release-defects-oct06`: reconcile
what has been proven, resolve the known evaluation failure, and make each business and
operational guarantee reviewable. It does not restart the production upgrade or rebuild
what exists. No push, deployment, hosted-CI dispatch, cloud or paid-model call is part of
this pass; everything here is local unless an entry says otherwise.

Status words are used strictly: **implemented** (in the code), **locally verified** (an
executable check passed on this machine with a record), **externally verified** (a hosted
run anyone can open), **blocked** (needs an input listed in the deferred checklist) and
**unmeasured**.

## Step 1 — source identity and evidence

**Starting point.** Clean tree on `codex/release-defects-oct06` at
`69c62de959a988305463be4b0219db7e88b1bcde`. The brief named `a05237b` as the last
executable candidate; the branch had moved on. Ancestry and the path class of every
change were checked with git:

| Commit | Role | Path classes changed after the previous row |
|---|---|---|
| `a05237b3e3621c6744c435e5007776adec3f46a1` | first measured candidate | — |
| `df567807fb8ec51a39a3aee138616de6139c233f` | documentation and evidence | documentation and evidence only (confirmed) |
| `7950e71304ec1c6da7985f6fa9ec41a2b154da6f` | **executable candidate** measured by the `r5-final-*` records | application (`app/logs.py`, `scripts/ingest.py`, `scripts/record_evidence.py`), tests, documentation |
| `7bca4ca78a20a33000b6c9fcfabd78e27552a43f` | CI and compose fixes | workflow, deployment configuration, documentation, evidence |
| `69c62de959a988305463be4b0219db7e88b1bcde` | documentation | documentation only |

The workflow and compose changes after `7950e71` are covered by hosted CI runs
37651718276 (`7bca4ca`) and 37653591687 (`69c62de`), both successful. The compose change
itself was not run through `docker compose`; its effect was reproduced with `podman run
--shm-size` (`r5-ci-shm-*.json`) and its syntax checked with `docker compose config`.

**Changed.** The evidence is now indexed from machine-readable inputs, not prose:

- `scripts/evidence_index.py` writes `evidence/index.json` and
  [EVIDENCE_INDEX.md](EVIDENCE_INDEX.md) from git, the code's version constants, the
  candidate's records, a JUnit file, the evaluator's detail files,
  `evidence/ledger.json` (the defects as data) and `evidence/external.json` (hosted
  runs, each checked against GitHub's record of its commit and conclusion). Every
  reference is resolved; what does not resolve is listed, never dropped.
- `evidence/test_categories.json` puts every test file in exactly one category — EXPLAIN
  planning, numerical oracle, authorization, durability, ingestion, observability,
  evaluation harness, unit semantics, rendering, release tooling — so the 672 EXPLAIN
  cases cannot be read as numerical correctness. `tests/unit/test_evidence_index.py`
  fails on an uncategorised or doubly categorised file, or a dangling ledger reference.

**Found.** For six of the seven October defects, the original failure on unmodified code
survived only as a hand-assembled summary (`r5-original-reproductions.json`) that hashes
four logs no longer present (they were in a temporary directory). The summary is kept and
the four logs are listed as missing. To give each defect a command record, its regression
files were re-run against the baseline `76298b6` application code
(`evidence/probes/replay_on_baseline.sh`; candidate tests, baseline code, every copied
file recorded by digest):

| Defect | Replay on `76298b6` | Failures |
|---|---|---|
| eval budget terminal | `r5-replay-eval-budget-terminal.json` | 6 failed, 27 passed |
| failed-attempt accounting | `r5-replay-failed-attempt-accounting.json` | 11 failed, 65 passed |
| ingestion boundaries | `r5-replay-ingestion-boundaries.json` | 9 failed, 98 passed (`InvalidOperation`, `OverflowError`, `NumericValueOutOfRange`, ...) |
| log allowlist | `r5-replay-log-allowlist.json` | 13 failed, 8 passed, 1 collection error |
| telemetry outage bounds | `r5-replay-telemetry-outage-bounds.json` | 11 failed, 24 passed, 1 setup error |
| evidence-recorder counts | `r5-replay-evidence-recorder-counts.json` | 1 failed; recorded with the baseline's own recorder, whose counts field shows the defect (`error: 680`) |
| CI startup refusal | `r5-replay-ci-startup-behaviour.json` + `.result.json` | the regression tests import the fix itself, so a behavioural probe ran the baseline app as the baseline CI did: no database, still running at the 25 s deadline (exit 124); owner credential, refused (exit 3); the baseline CI's grep matched neither |

The JUnit files split failures into interface (the baseline lacks something the test
calls) and behavioural (wrong result or crash), by exception type; the split is
approximate and labelled so in the index.

**Incident during this step.** On baseline code an `AttributeError` printed the settings
object's repr, which includes the local database role passwords; pytest wrote it into a
JUnit file and it appeared once in this session's terminal output. Those JUnit files were
deleted before any commit, and every JUnit file kept as evidence now passes through
`evidence/probes/sanitize_junit.py` (exception type and a scrubbed first line only; no
traceback, captured output or host name). Every evidence file was checked against the
`.env` secret values: none. The exposed value is a local, disposable cluster's role
password, never committed or pushed; rotating the local role passwords is recommended.

**Verification.** `python3 scripts/evidence_index.py --candidate 7950e71`: 16 candidate
records, 10 defects, 0 unresolved problems, 5 missing (the candidate's full-suite JUnit,
to be produced on this pass's final candidate, and the four unpreserved logs).
`pytest tests/unit/test_evidence_index.py`: 2 passed.

**Residual.** Hosted CI is externally verified for `7bca4ca` and `69c62de` only. The
candidate's JUnit is not yet in the index.

## Step 2 — the offline k-07 mismatch

**Diagnosis.** Intent mapping plus a missing capability; not an obsolete oracle,
and not a genuinely ambiguous question (it is a supplied sample question). The
offline planner read "growing or declining" as two-window growth and "month over
month" as a monthly breakdown; the compiler refuses that pair, so the answer was a
clarification, scored `wrong`. The live prompt carried the same rule. No plan could
express each period against the one before it. The refusal also said "mo-by-mo".
Full write-up: [EVALUATION.md](EVALUATION.md#k-07-7-october-2026-each-period-against-the-one-before-it).

**Changed** (implemented, locally verified): `AnalyticalPlan.period_over_period`
(planner contract 2.1.0), compiled on the dense calendar series with `lag` (added to
the SQL validator's allowlist alone; `lead` and `nth_value` stay refused); each row
carries the prior period, the change, the percentage, both periods' week counts and
whether the period is still accumulating; the headline answers the direction and
marks a provisional period. The offline planner and the live prompt (2.2.0,
fingerprint `ed8e49619d32de7b`) distinguish a series, each period against the one
before, a two-window comparison and a rolling average. The table shows the new
columns. Semantics are recorded as [ASSUMPTIONS.md](ASSUMPTIONS.md) A20.

**Verification.** Reproduction `r5-k07-reproduced.json`: 25 failed on unmodified
code. Now: 19 unit, 7 fixture-database numerical tests against an oracle computed
outside the compiler, 3 end-to-end tests of the sample question (national and
territory-scoped, nonempty), 3 component tests. Mutants, each caught: no period
before the window (fails 4), a percentage from any nonzero prior (2), unknown
months imputed as zero (6), no partition by group (1), cadence not read as a series
by the planner (7). Offline evaluations: 38/38, 12/12, 12/12 (holdout2 was 11/12;
it was already spent and stays spent, so 12/12 is a regression result). No oracle
changed.

**Residual.** No live model has run under prompt 2.2.0 (nor 2.1.0). The evaluation
oracle checks the plan only; numbers are checked by the tests. Two-window
`volume_growth` still divides by a negative prior window without special handling
(A20; needs owner agreement to change). Per-week normalisation of uneven months is
disclosed, not applied.

## Step 3 — numerical and unseen-data evaluation

**Coverage by invariant** (implemented, locally verified). `evidence/invariants.json`
maps 19 business invariants to the tests that check each against an independent
oracle; [COVERAGE_BY_INVARIANT.md](COVERAGE_BY_INVARIANT.md) is rendered from it and
`tests/unit/test_invariant_coverage.py` fails on a dangling reference or a stale
report. EXPLAIN-only planning cases, offline evaluations, the live-adapter fake and
the (never run) live provider are listed separately as not numerical. One gap stays
open: no test compares an answer's values across a republication.

**New oracles, and what they found.** Revenue values, the 340B share, the segment
share and quarter and week series had been executed but never compared with a number
worked out another way (`tests/integration/test_invariant_oracles.py`). The segment
share was wrong: with no market named, its numerator covered every market and its
denominator only ours, so the generic share of our markets read **59.67% instead of
26.43%**, and a market outside ours showed a numerator with a blank share; rows from
the denominator side alone also lost their labels. Reproduced (`14ce17e`,
`r5-ratio-population-reproduced.json`), fixed (`eb3ba13`); two mutants caught.

**Fixture profiles** (implemented, locally verified). `scripts/fixture_profile.py`
generates two deterministic same-schema datasets unlike the supplied one -- names,
id formats, skew, cardinality, market-data gaps, a silent month, unknown
classifications, partial hierarchies, an ISO week-53 history -- with a 98-event
ingestion series (replays, equal-valued distinct sales, a correction, tombstones in
and out of order, a conflicting version, refused records, a rejected batch, an
anchor move). Seed, generator and contract versions, true classifications, expected
event outcomes and file hashes are in each manifest. `scripts/build_profile_db.py`
builds one through the ordinary bootstrap, load and ingestion with its own users and
writes a reconciliation report: on both profiles every event reconciles and totals
move by exactly the applied changes. `tests/integration/test_fixture_profiles.py`:
20 checks on both profiles for Exec, director and RAM (nonempty scopes).

Building the profiles found two defects:

- **Scope widened by a reused territory name.** Scope binds by name; a geography
  reusing one name for two territories loaded, and a RAM assigned it saw both.
  Reproduced (`51cf05e`), refused at load (`fa02451`).
- **Week labels across a week-53 year end.** The supplied generator labels weeks with
  the Saturday's calendar year; the bulk load accepts that and ingestion then refuses
  to extend the calendar. Recorded here; the loader check is part of step 4.

**Judge.** Five more adversarial cases: every wrong shape for k-07 still fails; the
monthly series passes with or without the per-month change.

**Blinded evaluation packet** (`evals/packet/`, implemented; not yet used): an
authoring guide that lists what an independent author may and may not read, an
oracle schema matching the judge, a contamination log, and a run protocol fixed in
advance (versions, budget, one run unless N is chosen first, categories reported
separately, Wilson intervals, slices, latency, known and unknown usage).
`scripts/check_question_set.py` checks a new set's schema and its overlap with the
development sets. Run on the existing sets, it found that **`holdout.yaml` was never
fully held out**: 4 of its 12 questions are identical to regression questions added
the day before, and one more is a near-duplicate. Its figures are kept, with a
correction in [EVALUATION.md](EVALUATION.md); `holdout2.yaml` has no overlap.

**Residual.** No independent author has written a set; nothing has been run against
a live model under prompt 2.2.0. The runner names one user per role, so an Exec
without pricing cannot appear in a question set. Profiles are synthetic.

## Step 4 — real-data contract

**Classification authority.** Classes did not come from an authoritative
mapping: rule 1.0.0 read the generator's ' GENERIC' / ' BIOSIMILAR' name
suffixes and made every other `brand_flag = 0` product a branded competitor by
elimination, on any dataset. Reproduced on the fixture profiles (`f26e8a8`,
`r5-classification-reproduced.json`, 7 failed): orchard's products of unknown
class became branded competitors, and the branded-competitor share of its
Anti-IL market read **50.2%** where 27.2% is known and 23.0% is of unknown
class; estuary, which ships no mapping, had every competitor classified by name.

*Changed* (implemented, locally verified; `3cc4b14`, `r5-classification-fixed.json`).
Rule 2.0.0: `brand_flag = 1` is ours (authority: source); any other class comes
from a versioned mapping (authority: mapping; version and SHA-256 recorded per
product), else `unknown`. The supplied dataset's mapping
(`app/data/classification_supplied.json`) is curated from the tables of
`docs/market_classification.md`; all 40 supplied products keep their class and
none is unknown. A contradicting, duplicated or invalid mapping refuses the load.
The manifest records classes, authorities and mappings (`source_coverage.classification`).
Metric-specific availability: only `market_segment_share` reads a class
other than ours; it now carries the market volume of unknown class and states
the share as the range it allows, has no share for a market whose volume is
all of unknown class (unavailable, not 0%), and treats a fully classified market
without the segment as a known zero. Other metrics are unaffected. Orchard now
reads 27.21% to 50.22%; estuary 0.00% to 69.33%. Metric registry 1.5.0, so
prompt 2.3.0 (fingerprint `5dd66431f2ba8230`; instructions unchanged). Eight
mutants, each caught (`evidence/probes/classification_mutants.sh`).

**Calendar convention at load.** A dataset whose week labels follow the
supplied generator's calendar-year convention across an ISO week-53 year end
loaded silently and refused its first batch (`b7aa31e`,
`r5-calendar-convention-reproduced.json`). The load now runs ingestion's own
convention check and records `source_coverage.calendar`; such a calendar still
loads, with a `calendar_not_extendable` warning (`2cc7bd9`,
`r5-calendar-convention-fixed.json`). The supplied dataset: extendable
(Saturday weeks, week-ending month).

**Readiness report** (implemented, locally verified; `00c192e`,
`r5-readiness-reports.json`, reports in `evidence/readiness/`).
`scripts/readiness_report.py` reports, per published dataset, identifiers,
periods, source totals and months lacking a source, units, money, classes,
hierarchy, territory access, cadence and correction identity, each `ready`,
`attention`, `blocked` or `not_measured` with its evidence, and the contract
thresholds, all marked provisional. Supplied data: attention (competitor-only
market source; WAC interpretation A8 needs the owner), cadence and correction
identity not measured (no batch ingested). Both profiles: attention (unknown
classes, months without market data, unmapped facilities), every event
reconciled. The relabelled week-53 dataset: blocked (periods).

**Ledger semantics** were already covered by the profiles' 98-event series
(replays, equal-valued distinct sales, a correction, tombstones in and out of
order, a conflicting version, refused records, a batch rejected whole, an anchor
move), each event reconciled against the ledger, quarantine and batch log.
**Real-feed acceptance**: [REAL_FEED_ACCEPTANCE.md](REAL_FEED_ACCEPTANCE.md),
twelve items with what is verified and what each needs from a feed or its owner.

**Residual.** No real feed, customer mapping or identity-provider claims have
been used; every item that needs them is marked blocked in the checklist. The
supplied mapping is only as good as the document it was curated from. The
evidence index lists five fixes after the measured candidate (`7950e71`) as not
yet verified on a candidate; step 8 measures the final one.

## Step 5 — audit guarantees

**Starting point.** Audit writes were best effort: the row was written after the
turn committed, in its own transaction; a failure was logged, counted and alerted
on, and the answer still released. That was documented and pinned by a test, with
two replacements described and neither built. One document overstated it
(`EVALUATION.md`: "any failed request: still written to the audit trail"), now
corrected.

**Decision** ([AUDIT_DECISION.md](AUDIT_DECISION.md)). Best effort stays the
default. A strict mode is implemented and opt-in (`PAC_AUDIT_MODE=strict`): atomic
*and* fail closed, because either alone still lets an unrecorded answer out. The
row commits in the transaction that commits the turn and the run's outcome; if it
does not commit, the answer is withheld (`503 audit_unavailable`, retryable under
the same key, nothing committed); a replay is recorded (`replayed`, `replay_of`)
before it is returned, or withheld. Every row now carries `run_id` and
`audit_mode` (migration 024). Implemented `1c5df01`, documented `ab8dde9`.

**Verification** (locally verified; `r5-audit-modes.json`, 40 passed;
`r5-audit-mutants.json`, 5 of 5 mutants caught). In both modes where they differ:
a refused insert, a storage outage at commit, a worker killed before, inside and
after the commit (a child process exiting without cleanup), a lost response, a
replay whose row cannot be written, an uncertain commit acknowledgement, a
duplicate request, an expired lease, access revoked before a replay, and a cancel
before the answering step or during the commit. Under best effort the tests pin
the gap the record states: a worker dying between the commit and the audit write
leaves a committed, replayable answer with no row, and its replay has none either.

**Incident during this step.** The first mutant run was started before the strict
mode was committed. The probe restores each mutated file with `git checkout`, which
discarded the uncommitted edits to `app/pipeline.py` and `app/conversation/state.py`.
Nothing committed was affected; the edits were re-applied from the same edit
scripts, the suite re-run (2209 passed) and committed before mutants were run again.
Both mutant probes now refuse a tree with uncommitted changes.

**Residual.** Which mode a deployment uses is the owner's decision. Strict mode
turns an audit-storage problem into an answer outage. Refusals before a run starts
are in telemetry only. Worker death was simulated as a child process against one local
PostgreSQL, not across containers (step 6).

## Step 6 — multi-process execution and a real telemetry backend

**Starting point.** Telemetry was tested against in-process exporters; the alert rules
existed as a table in `OBSERVABILITY.md`, never evaluated by Prometheus, and no test ran
more than one application process.

**What was built** (implemented `d7263c4`, extended `4ef0e50`, `866b6e2`).
`deploy/observability/` holds the collector configuration (OTLP/HTTP in, Prometheus
exporter out), a Prometheus configuration, the 16 alert rules as Prometheus evaluates
them (`alerts.yml`; the documented table is generated from it and a test keeps them
equal and checks every metric a rule or panel names is exported) and the dashboard
(`dashboard.json`). `scripts/fetch_ops_tools.sh` fetches the official Collector contrib
0.162.0 and Prometheus 3.15.0 builds, pinned by the SHA-256 their projects publish.
`scripts/ops_drill.py` is the one reproducible script: it creates its own PostgreSQL
cluster (`initdb`, own port, credentials generated in the run and never printed), loads
the seed data, starts the collector (with `otel-collector.local-files.yaml` for the files
it scans), Prometheus and two uvicorn processes with separate pools, runs twenty
scenarios and tears everything down. Alert windows are shortened by substitution only,
and the run lease and request deadline to 15 s and 12 s (both listed in the detail file).
Barriers are a held table lock observed in `pg_stat_activity`, or a row observed in the
database, never a sleep standing in for one.

**Reproduced first.** Each finding below failed on code without its fix.

| Finding | Reproduction | Effect | Fix |
|---|---|---|---|
| The resource was `service.name` and `service.version` only | `c2e5915`, 1 failed | Two processes wrote one series and overwrote each other's running totals: request totals, six statement timeouts and the totals after the collector outage each held one process's share | `4187419`: a random `service.instance.id` per process |
| A counter series first appears at its first increment | `b974af7`, 3 failed (with the next two) | `increase()` has no earlier sample: the first audit loss never paged `AuditLoss` | `4187419`: the three single-event counters start at 0 |
| Feed alerts read the jobs process's counters | as above | A one-shot process exports once and its series expire: `BatchRejected` never fired | `4187419`: two gauges read from the batch log by the serving processes; migration 025 indexes it |
| Exporters return `FAILURE` for an unreachable collector; the wrappers logged only exceptions | as above | A 35-second collector outage left no line in either process's log | `4187419`: logged, at most once a minute per exporter |
| The deployable collector configuration wrote every span and metric to local files (to `/tmp` unless `PAC_OTEL_FILE_DIR` was set), against its own header; the file exporter truncates at each start | `952c6a9`, 2 failed; probe: the span sent before a restart was gone after it | The 13-of-13 drill run's sentinel scan read 53 KB of traces: only what was exported after its collector restart, not every exported trace as this record first said | `fba626c`: no file exporter in the deployable configuration (traces to `debug`, counts only, until a backend is chosen); the drill's overlay appends. The scan now reads 0.4 MB of traces |
| A database error was logged with type `Exception` | `7366937`, 1 failed | The refused audit insert could not be told from a lost connection in the log | `9fb1d1f`: the driver's SQLSTATE-named classes are kept (`InsufficientPrivilege`), still never the message |

The drill on `b974af7` (`r5-ops-drill-before.json`): **6 passed, 7 failed**; after
`4187419` and one drill bug (`ed02e4f`): 13 of 13 (`r5-ops-drill-after.json`), with the
scan limitation above.

**Verification** (locally verified; `866b6e2`, clean tree, `r5-ops-drill-final.json`,
**20 passed, 0 failed**; detail in `r5-ops-drill-final.detail.json`):

- promtool accepts the production rules, the drill rules and the configuration;
- traffic of every outcome (answered, clarify, denied) on two processes: distinct
  instances, Prometheus totals equal the requests each process received;
- the per-user rate limit holds across processes (the 21st request: 429), and deleting
  every conversation refunds nothing (20 answered, 20 deleted, the next 429);
- one idempotency key on both processes while the first holds it: 409
  `same_request_running`, then the identical answer replayed;
- **a process killed** (`SIGKILL`) while its request waited on a lock: the same key on
  the other process is refused while the lease lives, then taken over and committed
  once (one turn, two attempts counted); the restarted process replays that commit;
- **two questions in one conversation** on two processes: the second is refused
  (`conversation_busy`) while the first holds it, then answered; turns 1, 2, 3, one run
  each;
- **a clarification** asked on one process and answered on the other: the second choice
  as shown is the one bound, and the paused thread is pruned;
- **pricing revoked** between an answer and its replay on the other process: 403
  `access_changed`; the history and a new answer show no figure;
- **overload and cancellation** on a third process with one query slot and one queue
  place: the overflow refused in 0.02 s (503, `Retry-After`), the queued request
  cancelled by its owner from another process (nothing committed), the refused one
  retried and answered;
- a refused audit insert pages `AuditLoss`; statement timeouts page `QueryTimeouts` and
  `Errors`; a rejected batch pages `BatchRejected` and clears when one is accepted; old
  events page `DataNotMoving`; a stopped feed pages `MissedIngestionRun`; a current batch
  clears both; both processes then answer from the same refreshed snapshot;
- both processes stopped: `FreshnessNotReported` pages and clears on restart;
- the collector killed for the length of 50 answers: answers continue (p95 0.035 s
  before, 0.023 s during), resident memory does not grow, each process logs the failed
  export, a process stopped meanwhile exits in 6.8 s (bounded flush), `TelemetryPipelineDown`
  pages and clears, and the totals catch up;
- every alert that fired clears; all 21 dashboard queries run, and only the four model
  panels are empty (no model is called);
- sentinels sent in (per-user e-mail tags, a question, territory names, a revenue
  figure, idempotency keys, session cookies, the database and user passwords) are found
  in no exported trace or metric (kept across the collector restart), Prometheus label
  value or log, with a positive control in the database;
- the logs: one JSON object a line, only registered event codes, the refused audit
  insert typed `InsufficientPrivilege`, and each of the 12 failed queries' lines carrying
  the response's `X-Request-ID` and a `run_id` whose trace records `QueryCanceled`.

**Concurrency budget** ([RUNBOOK.md §9](RUNBOOK.md)): admission limits are per worker
process and multiply with workers and replicas; per-user rate and concurrency are
counted in PostgreSQL and do not. Both are shown above on separate processes.

Not exercised, and why, is in the detail file: `SlowAnswers`, `ModelFailing`,
`UsageNotReported`, `PoolSaturation`, `PoolExhausted`, `DatabaseErrors`,
`TurnsNotSaved`, `QuarantineRising` (their expressions are still checked by promtool and
by the metric-name test).

**Observations, not changed.** `FreshnessNotReported` fires on a deployment that has
never accepted a batch (seed data only): right for a feed deployment, noise for a
demonstration. `Errors` is a ratio and needs a sustained condition. `increase()` still
cannot see the first sample of a series whose label values are not known in advance
(per-model or per-source); the zero start covers the three single-event counters. The
drill's application processes hold the owner credential (local mode); a deployed
serving process does not.

**Residual.** Local evidence: one machine, one PostgreSQL cluster, processes (not
containers, so no container restart, network partition or orchestrator), a local
collector and Prometheus with shortened windows and lease, no Alertmanager routing, no
hosted backend, no real identity provider, and not an on-call drill or a load test. The
container image was not run in the drill. A hosted collector and backend need
credentials and a target; blocked, in the checklist.

## Step 7 — performance, rollback and recovery against the candidate

**Targets** ([TARGETS_DECISION.md](TARGETS_DECISION.md)). Provisional service,
freshness and recovery targets, each labelled an assumption with the measurement it is
judged against. No SLO is declared; the owner's agreement is in the deferred checklist.

**Performance under row-level security** (locally verified; `1e0312a`, `r5-load-profile.json`,
machine held awake). HTTP sessions through the scoped and exec roles: RAMs in the busiest
territories (the skewed sources), Directors and Execs (60/30/10); cheap, medium and
expensive shapes (45/40/15) including the dense monthly series under RLS; two workers,
the image's pools, admission on; the offline planner; 30 s per level on 2,000,000 sales;
a 10-core, 16 GiB machine with 8.5 GiB of swap used by other applications. At 8 clients:
21.9 answers/s, p95 cheap 0.35 s, medium 0.78 s, expensive 4.45 s, no errors. At 16:
24.3/s, no errors. At 32: 22.3/s, one statement timeout (0.14%). At 64: 18.3/s answered,
31.5% refused at once (`503 overloaded`, refusal p95 0.10 s), two timeouts (0.23%).
Publications under load: in-week 32.6 s, new week **320 s** (434 s on 1 October), readers
answering throughout (p95 2.12 s during against 2.01 s outside; 0.57% expensive
timeouts). This is offline-pipeline capacity; provider-inclusive capacity is unmeasured.
An earlier attempt at the same commit spanned a system sleep (`r5-attempt-load-profile-slept.json`):
its concurrency levels agree, its publication phases are not used.

**Soak** (`1e0312a`, `r5-load-soak.json`): 8 clients for 10 minutes right after the
new-week batch, to answer two concrete questions. Does anything drift as the per-request
rows (attempts, audit, conversations) accumulate? No: p95 2.28–2.40 s every minute, worker
memory flat near 145 MB, 8,000 attempts added linearly. Does the batch leave a lasting
cost? Yes: it leaves the facts table and its indexes at twice their size (heap 356 →
711 MB, indexes 776 → 1,550 MB), and expensive questions timed out at 0.99%.
`VACUUM (FULL, ANALYZE) sales` (`262c7cd`, `r5-compact-after-shift.json`) took 33 s with
readers blocked and restored both; at 8 clients errors then fell to 0.11% and p95 to
1.58 s (`r5-load-after-compaction.json`), the throughput of a never-published copy
measured two minutes later (14.6 against 15.5 answers a second, `r5-load-control-fresh.json`).
The soak's lower throughput than the profile's (13.4 against 21.9) is mostly this machine's
variance, which the control exposed; CAPACITY.md's 1 October reading of a "within
variance" dip after the batch was half right. Compaction in the batch's out-of-hours
window is now a working assumption, not a code change.

**Rollback** ([ROLLBACK_DECISION.md](ROLLBACK_DECISION.md)). No previous image is a safe
target: the previous executable candidate, `7950e71`, lacks the territory-name scope fix
(an access defect) and the numerical fixes after it, and every earlier one has more. The
strategy is forward fix; restore is for lost or damaged data. The compatibility of the
previous code with the upgraded schema was measured all the same, in a cluster of its
own (`d2a6628`, `r5-upgrade-compatibility.json`): the candidate's migrations upgrade a
database the previous release left with live state, twice, converging; the candidate
serves it with that state intact (an old session, a replayed answer, a resumed
clarification, a dead run taken over after its lease and committed once, RAM rows, no
pricing for a no-WAC executive, SSO disabled to password only). The previous code also
serves it, but writes audit rows without `run_id`/`audit_mode` and cannot load data
(`NotNullViolation` on the 023 `authority` column; the published dataset is untouched).

**Restore into a new cluster.** The documented procedure was wrong. RUNBOOK §8 said to
run `bootstrap_db.py` before `pg_restore`; reproduced against a new cluster with a
full-size copy carrying application state (`4be5d4e`, `r5-restore-new-cluster-reproduced.json`):
`pg_restore` exits 1 with 142 errors, and every conversation, turn, run, attempt and
clarification (80 conversations) is missing, while the database reports ready and
answers questions; the stored answer is recomputed rather than replayed and the paused
clarification is gone. *Changed* (`6ced978`): `bootstrap_db.py --roles-only` creates the
roles and memberships only, and `pg_restore --create` recreates the database as dumped;
RUNBOOK §8 documents it and warns against the old order. Verified
(`r5-restore-new-cluster-fixed.json`): a new cluster (`initdb`, own port and
credentials), a 39.8 MB dump of 2,000,000 sales restored in **11.6 s**, restore to ready
with every check in **17.0 s** — row counts, generation, policies and RLS flags, every
table and column ACL (catalog digests), role memberships, CONNECT for the three logins,
the runtime boundary, readiness, the same answers as a RAM and an Exec, the stored
answer replayed, the paused clarification resumed. **Data cutoff restored**: the newest
audit row, turn and run equal the source's at the dump (to the microsecond); nothing
after the dump began. Three harness defects were fixed on the way and are kept in
history (`0f1920c`, `4a9de63`, `4be5d4e`); the failed attempts are kept beside the
records.

**Residual.** One development machine; the database on the same host; the offline
planner (no provider-inclusive capacity measured); a closed loop with no think time and
quotas lifted; a dump on local disk restored into a cluster on the same host — not PITR,
not off-host backup storage, not a managed database, not a production RTO or RPO; the
previous release's source run as processes, not its image; no deployed rollback or
roll-forward.

## Step 8 — release handoff

**CI path** ([RELEASE_HANDOFF.md](RELEASE_HANDOFF.md)). `ci.yml` runs on pushes to
`codex/**`, so this branch is covered, and on pull requests and `workflow_dispatch`. The
four jobs (`test`, `frontend`, `supply-chain`, `image`) carry no branch or path
condition; the held-out evaluations are measurements by design; the image job builds
with `push: false` and publishes nothing. One gap was found: the full-suite step ran
without the release gate, so the fixture-database tests (the k-07 regressions among
them) skipped on an empty fixture and the step stayed green (`cff458e`,
`r5-ci-full-suite-gate-reproduced.json`). It now runs under `--release-gate` (`03316f3`,
`r5-ci-full-suite-gate-fixed.json`); locally the whole suite satisfies it. Hosted CI
passed on `69c62de` (run 37653591687), the last commit on GitHub; for the local commits
after it, hosted CI is **pending** authorization to push. The current-branch
instructions in README, REQUIREMENTS and RELEASE_EVIDENCE named
`post-assessment/release-risks`; corrected.

**The executable candidate, `84dfc1e`** (locally verified, from a clean tree and freshly
provisioned databases; `r5-candidate-*.json`): the boundary; security 433 and ingestion
177 under the strict gate; the whole suite under the strict gate, **2222 passed, no
skips, no xfails** (JUnit sanitised, `r5-candidate-pytest.junit.xml`); 832 unit tests
with no database; the release gate's self-test; the offline evaluation sets 38/38,
12/12 and 12/12 (the holdouts are spent: regression measurements); 32 components on
Node 24; the web build; 10 browser journeys in Chromium; pip-audit, `npm audit` and
gitleaks over the history, clean; the linux/amd64 image, 25 checks (emulated with
Rosetta in an arm64 podman VM).

**Release manifest** (`evidence/runs/r5-manifest-84dfc1e.json`, `scripts/release_manifest.py`):
the commit and tree; the SHA-256 of `requirements.lock`, `pyproject.toml`,
`web/package-lock.json`, `web/package.json`, `Dockerfile` and `.dockerignore` as
committed; the contract versions and prompt fingerprint (`5dd66431f2ba8230`); each
record's SHA-256, the commit it measured, clean or not, gate or measurement, outcome;
the image's **config ID** `263a2296d35b…` (the local engine's image ID, the SHA-256 of its
configuration; the same ID trivy scanned) and its **manifest digest** `sha256:3e3ed288…`
(the manifest in the local store; **not** a registry digest: nothing was pushed; no OCI
index for a single-platform build); trivy 0.58.1 with its database updated
2026-10-07 07:38Z and downloaded 2026-10-08 08:44Z; the gate (no HIGH or CRITICAL with a
fix: passed, 0 findings) and the complete counts, none fixable: HIGH 44, MEDIUM 58, LOW
61, UNKNOWN 2, CRITICAL 0; the reviewed exceptions (two exact gitleaks fingerprints,
SUPPLY_CHAIN.md); the last hosted run; and the paths after the candidate, each classed:
**no build input changed**. The scan reports are `evidence/runs/r5-scan-84dfc1e-trivy-gate.json`,
`-trivy-full.json` and `-trivy-db.json`. The policy statement stays what the gate establishes: no
HIGH or CRITICAL vulnerability with a fix available; 44 HIGH without a fix remain in the
base image's packages.

**Documentation from the evidence index.** `docs/EVIDENCE_INDEX.md` is regenerated
from the candidate's records and JUnit: 16 records (the image's among them), 21 defects
in the ledger, 0 problems; 4 hashes of original logs from the 6 October review that were never
preserved, stated as missing.

**History review, and one more finding.** Before bundling, gitleaks ran over every
commit reachable from HEAD, not only up to the candidate. It found the base image's
`GPG_KEY` four times in the candidate's own trivy reports, committed in `ef061f1`
(`47bf736`, `r5-head-gitleaks-reproduced.json`): the public CPython release key, the
value already reviewed as not a secret, but enough to fail CI's supply-chain job on a
push. Four exact fingerprints were added to `.gitleaksignore` (`bbc2242`), and gitleaks
over the whole history then passed (`r5-head-gitleaks-fixed.json`). That file is the
one input changed after the candidate, and the manifest lists it: it changes only the
secret scan's exceptions, so the scan is the one check rerun at HEAD; no code, test,
dependency or image input changed. No `.env`, dump, key or dataset is in any commit
(`.env.example` holds empty values); the largest file is 0.5 MB.

**Bundle.** A Git bundle of `codex/release-defects-oct06` at the final HEAD, its
SHA-256 and an inventory are produced beside the repository (a file inside the bundle
cannot carry the bundle's checksum). `git bundle verify` reports it complete.
Verification commands are in RELEASE_HANDOFF.md.

**Deferred checklist**: [RELEASE_HANDOFF.md](RELEASE_HANDOFF.md#deferred-checklist).

**Residual.** No push, hosted CI run, registry publication or deployment was made, so
nothing after `69c62de` is externally verified, including the stricter CI step. The image
was built and run under emulation on a developer machine, not by a hosted runner. Branch
protection on GitHub was not inspectable from this checkout.

## Release verification — 8 October 2026

A verification pass over steps 1–8, against the actual candidate, without repeating the
implementation program. External steps are prepared, not run:
[EXTERNAL_VERIFICATION_PLAN.md](EXTERNAL_VERIFICATION_PLAN.md).

**Identity.** The handoff bundle (`37199f2`, SHA-256 `a8cd1298…0341a`) verifies: checksum,
complete history, one ref; `69c62de` and `84dfc1e` are ancestors of `37199f2`. Between
`84dfc1e` and `37199f2` the only path outside documentation and evidence is
`.gitleaksignore`. Every executable image input (`app`, `migrations`, `schema`, `scripts`,
`web`, the lock, `pyproject.toml`, `Dockerfile`, `.dockerignore`) has the same Git tree at
both; `README.md`, which the image copies, and the revision label do not, so a build at
`37199f2` would be a replacement image needing its own scan and journeys.

**The release gate.** `03316f3` changes only the CI command (`--release-gate` on the full
suite); no assertion changed. The gate's 19 self-tests fail a run on a skip in any phase
(setup, call, `skipif`, module level), an unapproved xfail or an xpass, an empty or
narrowed selection, a collection error, a session where nothing passed and a teardown
failure; the CI step itself fails on an empty fixture database
(`r5-ci-full-suite-gate-fixed.json`). **k-07** has blocking coverage at the exact question:
`tests/unit/test_period_over_period.py` (the planner) and
`tests/integration/test_period_over_period_pipeline.py` (the pipeline), both in the strict
suite; `holdout2` stays a measurement. No fresh independent holdout exists: it needs an
author who has not seen the development sets (`evals/packet/`).

**The post-candidate configuration change.** `bbc2242` adds four exact
`commit:file:rule:line` fingerprints, all the public CPython release key in the
candidate's own trivy reports. `evidence/probes/gitleaks_exceptions_exact.sh` (`a2b38d5`,
`r5-gitleaks-exceptions-exact.json`) plants, in a throwaway clone, the same public value in
a new file, a value of the excepted rule at an excepted path and line in another commit,
and an AWS-key-shaped value: all three are reported. The exceptions hide nothing else.

**Supply chain.** The unfiltered scan of the tested image has 165 findings, none fixable
in Debian: 44 HIGH, 58 MEDIUM, 61 LOW, 2 UNKNOWN, no CRITICAL, none in Python packages. The
44 HIGH are 8 advisories in 17 base-image packages; each is triaged in
[VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md) (reachability, mitigation, owner,
expiry). The gate ("no HIGH or CRITICAL with a fix") passes; that is not "vulnerability
free". The bases' resolved IDs and digests, the layer match and the 87 Debian packages are
recorded (`r5-image-84dfc1e-build-inputs.json`, `r5-image-a9de92e-build-inputs.json`);
pinning the bases by digest is proposed, not done.

**A new finding, fixed.** The tested image carried eleven setuid or setgid binaries,
among them setuid-root `mount`, `umount`, `su` and `newgrp` from util-linux, which made its
HIGH advisories reachable from the application user (`3389045`,
`r5-image-setuid-reproduced.json`, against the `84dfc1e` image `263a2296…`). `42e9090`
clears every setuid and setgid bit and adds the check to the image smoke and CI's image
job. That first check searched as `appuser`, whose `find` exits 1 on unreadable
directories, and ended the smoke silently (`r5-attempt-image-42e9090-setuid-check.json`);
`a9de92e` searches as root and fails on a failing search. The image built from `a9de92e`
(`b6d529719b31…`) has none (`r5-image-setuid-fixed.json`). Ledger: `image-setuid-binaries`.

**The candidate is now `a9de92e`** (locally verified, clean tree, freshly provisioned
databases; `r5-rc-a9de92e-*.json`, manifest `r5-manifest-a9de92e.json`): boundary; security
433 and ingestion 177 strict; the whole suite strict, 2222 passed, no skips or xfails; 832
unit tests without a database; the gate's self-test; offline evaluations 38/38, 12/12,
12/12 (regression and spent sets, not model accuracy); 32 components; the web build; 10
browser journeys; pip-audit, npm audit and gitleaks clean; the linux/amd64 image, 26
checks. Its application, migrations, schema and locks are identical to those of `866b6e2`
(the 20-scenario drill), `1e0312a` (load profile and soak), `6ced978` (new-cluster restore)
and `d2a6628` (upgrade compatibility), so those measurements describe its code.

**Hosted gates.** Not run: a push was requested by the owner on 8 October 2026 and refused
by the working session's permission controls. `main` has no branch protection and no
rulesets (read-only API).

| Item | Status | Exact identity |
|---|---|---|
| Hosted CI, all four jobs | **independently verified for `69c62de` only** | GitHub run 37653591687 |
| Hosted CI on the candidate and HEAD | blocked: push | — |
| Branch protection on `main` | blocked: absent; needs settings authorization | — |
| Bundle integrity and ancestry | locally verified | `37199f2`, SHA-256 `a8cd1298…`; the final bundle is beside it |
| Strict release gate, k-07 blocking coverage | locally verified | `a9de92e`; macOS arm64, PostgreSQL 16.14, Python 3.13.2 |
| Python suites: 2222 strict, security 433, ingestion 177, 832 without a database | locally verified | `a9de92e` |
| Offline evaluations 38/38, 12/12, 12/12 | locally verified; **not** model accuracy | `a9de92e`, offline planner |
| Components 32, web build, browser journeys 10 | locally verified | `a9de92e`, Node 24.19.0, Chromium |
| pip-audit, npm audit, gitleaks; exceptions exact | locally verified | `a9de92e`, `a2b38d5` |
| Image journeys 26/26, no setuid, trivy gate | locally verified | image `b6d529719b31…` (linux/amd64 under Rosetta, podman 5.7.1) |
| HIGH findings triaged | locally verified; release owner not named | trivy 0.58.1, database 2026-10-07 07:38Z |
| Multi-process drill (20) | locally verified at `866b6e2`; same application code | one machine, processes |
| Load profile, soak, compaction | locally verified at `1e0312a`; same application code | Apple silicon, 16 GiB, under memory pressure |
| New-cluster restore (17.0 s), upgrade compatibility | locally verified at `6ced978`, `d2a6628`; same code | local clusters |
| Live deployment (`7aae7cf`), live evaluations (September, earlier prompts) | reported only, historical | — |
| Four original logs from the 6 October review | missing (hashes only) | — |
| Registry publication and digest | blocked | — |
| Live model evaluation of prompt 2.3.0; fresh holdout | blocked: credentials, budget, independent author | — |
| Real IdP, real feed, staging, replicas, hosted collector and alerts | blocked | — |
| Targets, audit mode, retention and residency | blocked: owner decisions | — |

**Recommendation.** Neither pilot nor production readiness can be claimed: the agreed
gates that would support either (hosted CI on the exact head with enforced branch
protection, a live evaluation, a real identity provider and feed, staging, and the
owners' targets) are not met. Locally, `a9de92e` is ready to push for hosted CI.
