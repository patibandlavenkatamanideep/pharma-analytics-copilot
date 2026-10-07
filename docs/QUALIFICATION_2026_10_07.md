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
are in telemetry only. Worker death was simulated in-process tree against one local
PostgreSQL, not across containers (step 6).
