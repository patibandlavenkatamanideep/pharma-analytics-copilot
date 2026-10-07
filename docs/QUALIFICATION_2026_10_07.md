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
