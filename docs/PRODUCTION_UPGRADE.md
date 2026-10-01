# Production upgrade — working record

Post-assessment work toward supporting **original data** on the assignment
schema, with reliable execution, auditable evidence and safe operations.

> **Superseded as the current state on 2026-10-01.** This document records
> Phase 0 and Phase 1, which ended at `85bac76`. The work on the 30 September
> review, Phases 2 to 8, and the branch's current verdict are in
> [RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md). The text below is kept as the
> record it was, with its own dated corrections.

It does not describe intentions as achievements: every claim points at a
record under `evidence/runs/`, and anything unmeasured says so.

---

## Baseline

| | |
|---|---|
| Review baseline | `c8aab5bdcd70a512cb0fa9a3a687f474d3b3eb29` |
| HEAD at start of this work | **identical** to the review baseline |
| Worktree at start | clean, 0 modified files |
| Branch | `post-assessment/production-readiness`, branched from `main` |
| `main` | untouched. No rebase, no force-push, no history rewrite |
| Submitted history | preserved — 66 commits, all reachable |

### Dependency versions

| | | | |
|---|---|---|---|
| Python | 3.13.2 | PostgreSQL | 16.14 |
| FastAPI | 0.115.6 | psycopg / pool | 3.2.3 / 3.3.3 |
| Pydantic | 2.10.4 | sqlglot | 26.2.1 |
| uvicorn | 0.34.0 | argon2-cffi | 23.1.0 |
| boto3 | 1.43.101 | anthropic | 1.8.0 |
| pytest | 8.3.4 | PyYAML | 6.0.2 |
| Node | v25.9.0 | | |

### Contract versions in force

| Contract | Version |
|---|---|
| Metric registry | `1.4.0` (digest `5a39a26aaa68db9e`) |
| Policy | `1.0.0` |
| Schema contract | `1.0.0` |
| Mapping | `1.0.0` |
| Classification rules | `1.0.0` |
| Prompt | `2.0.0` (was unversioned) |
| Planner contract | `2.0.0` (was unversioned) |
| **Graph** | **does not exist** |

Prompt and planner contract were unversioned at baseline and are now `2.0.0`.
The graph does not exist yet and stays an explicit null in every evidence
record until it does.

### Baseline reproduction

`evidence/runs/p0-baseline-suite.json` — **377 passed, 0 failed, 0 skipped**
on `pharma_analytics` (dataset `full-182fd9082327`, fingerprint
`cba52562c89a4f8fd76c52a3`), offline provider mode.

| Suite | Collected |
|---|---:|
| `tests/unit` | 204 |
| `tests/integration` | 58 |
| `tests/security` | 115 |
| **Total** | **377** |

The reviewer's *"204 passed with the database conftest disabled"* is exactly
the unit suite. Both figures are correct; they measure different things.

**Not reproduced in this phase:** web component tests, Playwright browser
tests, the container image job, and any paid-inference evaluation. The
deployed service was not contacted or changed.

---

## Repository inventory

Coverage notes state what was actually read, not what exists.

| Path | Coverage |
|---|---|
| `README.md`, `DESIGN.md` | Read. Test counts conflict with current reality — Phase 1D. |
| `docs/ASSIGNMENT_README.md` | Read. Supplied, unmodified. |
| `docs/metric_definitions.md`, `data_source_guide.md`, `org_hierarchy.md`, `market_classification.md`, `account_analytics.md`, `product_analytics.md`, `period_offsets.md`, `security_model.md` | **Supplied domain documents.** Read during earlier phases and treated as the semantic authority. Unmodified; attribution preserved. |
| `docs/ASSUMPTIONS.md` | Read. 19 assumptions, A1–A19. |
| `docs/DATA_QUALITY.md`, `EVALUATION.md`, `REMEDIATION.md`, `REQUIREMENTS.md`, `RUNBOOK.md`, `AI_SYSTEM_DESIGN.md`, `DEMO.md`, `DEMO_SCRIPT.md` | Read. Internal inconsistencies catalogued below. |
| `.github/workflows/ci.yml` | Read in full. Gate defect confirmed at line 77. |
| `app/analytics/` | Read: registry, plan, periods, compiler, validator, render, entities, intent. |
| `app/auth/`, `app/conversation/`, `app/api/`, `app/pipeline.py`, `app/db.py`, `app/config.py` | Read. |
| `app/llm/planner.py` | Read in full. Prompt construction defect confirmed. |
| `app/data/` | Read: loader, manifest, schema_contract, classification. |
| `migrations/` | 8 files, read. Additive; supplied DDL unmodified. |
| `tests/` | Structure and the security/gate paths read; not every assertion. |
| `scripts/` | 8 scripts, read. |
| `infra/`, `Dockerfile`, `compose*.yaml`, `Caddyfile` | Read. Not executed in this phase. |

Reading an inventory is not reviewing contents; the table distinguishes the
two.

---

## Findings

Three categories, deliberately separated.

### Reproduced defects — a probe demonstrates them at this revision

| ID | Defect | Probe | Evidence |
|---|---|---|---|
| **D1** ✅ | *Fixed in Phase 1B.* The release gate accepted a skip raised from a test body. `tests/release_gate.py` converts a skip to a failure only when `report.when == "setup"`. A `pytest.skip()` inside a test body skips at **call** phase, so `--release-gate --min-tests 1` exits **0** for a run that verified nothing. | `evidence/probes/gate_accepts_call_phase_skip.py` | `p0-defect-gate_accepts_call_phase_skip.json` |
| **D2** ✅ | *Fixed in Phase 1A.* The live prompt mistyped cohorts and mislabelled turns. `build_system_prompt` emits *"The previous answer was about these **account ids**: ZENOVAX, GEMTARA"* for a **product** cohort, and *"This is a FOLLOW-UP"* whenever any previous plan exists. The typed-cohort work landed in `OfflinePlanner` and the pipeline but never reached the only text the live model sees. | `evidence/probes/live_prompt_mistypes_cohort.py` | `p0-defect-live_prompt_mistypes_cohort.json` |
| **D6** ✅ | *Found and fixed before closing Phase 1.* Cohort completeness was derived from the 200-id storage cap alone, so a response truncated by `max_result_rows` was recorded as **complete** whenever fewer than 200 ids came back. Latent under the default cap of 5,000 — any response truncation also exceeded 200 — but `max_result_rows` is configuration. The cohort was also built from the raw rows, so it included the extra probe row the renderer discards and never shows, and it stated the truncated row count as an exact population total. | `tests/unit/test_cohort_completeness.py` | `p1d-cohort-completeness.json` |
| **D5** ✅ | *Found and fixed in Phase 1D.* The 6 jsdom component tests could not be run from this checkout: the vitest worker started, never responded, and the run ended after 60 s having collected nothing. Recorded since the remediation phase as environmental — macOS stalling reads under `~/Desktop` — on evidence that never separated the path from the build cache. The cause was a stale `web/node_modules/.vite` entry. | see *The browser suites* below | `p1d-suite-browser-component.json` |
| **D4** ✅ | *Found and fixed in Phase 1D.* The evidence recorder truncated the first changed-file path. `_git()` strips its output before the caller splits it into lines; `git status --porcelain` starts each line with a two-character status field whose first character is a space for an unstaged change, so `app/config.py` was recorded as `pp/config.py`. Five records written in Phases 1A–1C carry it; they are listed under *Phase 1D* below and were left as written rather than re-recorded against a SHA they did not measure. | `tests/unit/test_evidence_record_accuracy.py` | `p1d-record-accuracy.json` |
| **D3** ✅ | *Fixed in Phase 1B.* CI did not run the strict gate. `.github/workflows/ci.yml:77` is `python -m pytest tests/security -q` with no `--release-gate` and no `--min-tests`. Confirmed by reading the workflow. | — | inspection |

### Source-level risks — the mechanism is in the code; production manifestation not reproduced here

| ID | Risk | Location |
|---|---|---|
| **R1** | Classification derives generic/biosimilar from **synthetic name suffixes** (`" GENERIC"`, `" BIOSIMILAR"`) and defaults every other non-company product to `branded_competitor`. On original data this silently manufactures a classification. Unknowns cannot currently stay unknown. | `app/data/classification.py` |
| **R2** ✅ | *Fixed in Phase 1C.* Shared mutable planner usage. `BedrockPlanner.last_usage` is instance state written per call and read later by `Pipeline`; one `Pipeline`/planner is created per process lifespan. Concurrent requests can read each other's usage, and plan repair overwrites first-attempt usage. | `app/llm/planner.py:229,303`, `app/pipeline.py:182`, `scripts/run_evals.py:493` |
| **R3** | No request-wide snapshot pin. The manifest, the vocabulary and the result rows are read in **separate transactions**; the vocabulary cache is keyed by `dataset_id` but reads current tables. A refresh between stages can label B's rows with A's calendar. Distinct from the already-fixed atomic publication. | `app/pipeline.py`, `app/analytics/entities.py` |
| **R4** | No original-data onboarding path. `load_data.py` accepts `seed`/`full` only, reads fixed generated paths, requires exact CSV headers, and loads while holding `TRUNCATE` locks. | `app/data/loader.py` |
| **R5** | Turn concurrency is protected at insertion, not across read→plan→answer. The advisory lock is taken inside `record_turn`, after planning. Two continuations can plan against the same stale parent. | `app/conversation/state.py` |
| **R6** | Per-statement timeout, no request-wide budget. Provider SDK retries plus plan repair can compound beyond any single limit. | `app/db.py`, `app/pipeline.py` |
| **R7** | Audit and conversation writes are best-effort — failures are logged and suppressed. | `app/pipeline.py` |
| **R8** ✅ | *Fixed in Phase 1D.* DSN built by raw string interpolation; a password containing `@`, `:`, `/` or `#` produces a malformed or misrouted URL. | `app/config.py:68` |
| **R9** | Observability is logs, aggregate timings and audit rows. No tracing, no metrics, no correlation across phases. | — |

### Proposed capabilities — not defects; work the brief requires

LangGraph orchestration, a bounded runtime harness, privacy-aware
observability, real-data onboarding contracts, and layered evaluation.

### Documentation inconsistencies (Phase 1D)

- Test totals differ across `README.md`, `docs/REQUIREMENTS.md` and historical
  `docs/EVALUATION.md` sections.
- README describes the release gate with flags CI does not pass.
- Some historical "unmeasured" and "not implemented" notes survive for the
  340B proportion and generic-share metrics, which now exist.

---

## Phase order

| Phase | Purpose | State |
|---|---|---|
| **0** | Baseline, inventory, evidence schema, reproduced defects | **complete** |
| **1** | Repair D1–D3, R2, R8; reconcile docs | **complete** (1A–1D, closing checks) |
| **2** | Original-data contracts, staging, versioned crosswalk | planned |
| **3** | Request-wide snapshot consistency (R3) | planned |
| **4** | Bounded runtime harness (R5, R6, R7) | planned |
| **5** | LangGraph orchestration | planned |
| **6** | Privacy-aware observability (R9) | planned |
| **7** | Layered evaluation | planned |
| **8** | Performance, UI, operations | planned |
| **9** | Evidence index and handoff | planned |

Phase 1 precedes any orchestration change: refactoring onto a graph while the
gate is unsound and the live prompt is wrong would make regressions
unattributable.

---

## Acceptance matrix

`blocked` means a prerequisite is unavailable. `not run` means deliberately
deferred. Neither is a pass.

| # | Acceptance item | Phase | State | Evidence |
|---|---|---|---|---|
| A1 | Reviewer can reproduce the baseline | 0 | **passed** | `p0-baseline-suite.json` |
| A2 | Every known gap recorded without conflicting claims | 0 | **passed** | this document |
| A3 | Evidence records carry SHA, versions, dataset, mode, limits | 0 | **passed** | `evidence/schema.json` |
| A4 | Strict gate rejects setup skips, call skips, empty and narrowed selections | 1 | **passed** | `p1b-strict-gate.json` |
| A5 | Live adapter prompt carries typed cohorts and explicit turn classification | 1 | **passed** | `p1a-continuity.json` |
| A6 | Planner returns an immutable per-call result; usage survives repair | 1 | **passed** | `p1c-planning-result.json` |
| A7 | DSN tolerates reserved characters | 1 | **passed** | `p1d-dsn-encoding.json` |
| A8 | Real-data mode loads configured paths with a readiness report | 2 | not run | |
| A9 | Unknown classification stays unknown and disables only affected metrics | 2 | not run | |
| A10 | A refresh cannot mix calendar A with rows B | 3 | not run | |
| A11 | Deadlines, cancellation, idempotency, duplicate-turn prevention | 4 | not run | |
| A12 | Clarification → restart → authorized resume | 5 | not run | |
| A13 | Cross-user checkpoint access rejected | 5 | not run | |
| A14 | Telemetry contains no sentinel sensitive values | 6 | not run | |
| A15 | Evaluator rejects wrong answers and unsafe trajectories | 7 | not run | |
| A16 | k-11 has measured evidence or a named limitation | 8 | not run | |
| A17 | Restore and rollback exercised locally | 8 | not run | |
| A18 | Model quality on original data | — | **blocked** | no authorized dataset; no paid inference |
| A19 | Deployed parity re-verified | — | **blocked** | deployment changes out of scope |
| A20 | A synthetic secret cannot reach an evidence record or a CI log | 1 | **passed** | `p1d-evidence-redaction.json` |
| A21 | Evidence records state the changed-file list accurately | 1 | **passed** | `p1d-record-accuracy.json` |
| A22 | No document states a test count that is not currently true | 1 | **passed** | `p1d-suite-total.json` |
| A23 | The browser component suite runs repeatably from this checkout | 1 | **passed** | `p1d-suite-browser-component.json` |
| A24 | The strict gate fails when a required database is removed | 1 | **passed** | `p1d-gate-requires-databases.json` |
| A25 | Cohort completeness distinguishes ranking, response and storage limits | 1 | **passed** | `p1d-cohort-completeness.json` |

---

## Phase 1B — the release gate (complete)

The gate now rules on **outcomes**, not phases:

| Rejected | Why it used to pass |
|---|---|
| Skip in **any** phase | Only `when == "setup"` was checked, so `pytest.skip()` in a test body slipped through |
| Module-level skip | Produces no items at all; `--min-tests` saw a smaller collection, not a violation |
| Collection error | Same — a module that fails to import shrank the collection silently |
| Unapproved xfail / xpass | A known-failing security test is a known-failing security requirement |
| Narrowed selection (`-k`) | The hook ran before pytest's own deselection, so it counted items about to be filtered out |
| A session where nothing passed | Zero of zero exited 0 |

`--allow-xfail` exists, is off by default, and has to be asked for.

**CI also never built `pharma_analytics_authtest`**, which `tests/security`
requires. Those tests have therefore been *skipping* in CI while the job went
green. The strict gate would now fail that job, so the workflow builds the
database and passes `--release-gate --min-tests 115`.

Verified: 134 passed under the strict gate (115 security + 19 gate tests);
`tests/unit/test_release_gate_strict.py` proves non-zero exit for each row
above, in a subprocess, because the thing under test is an exit code.

---

## Phase 1A — shared conversation semantics (complete)

The same question was answered twice, in two places, with two different
answers. `OfflinePlanner` had typed cohorts; the prompt — the only text a live
model sees — had none, and no test exercised the prompt.

`app/conversation/continuity.py` now resolves continuity once,
provider-independently, and both planners consume the result.

| Behaviour | Before | Now |
|---|---|---|
| Turn classification | any previous plan ⇒ `"This is a FOLLOW-UP"` | `fresh_question`, `follow_up`, `correction`, `clarification_answer`, `ambiguous_continuation`, each said explicitly |
| Cohort typing | every grain ⇒ `"account ids"` | typed; the prompt names the matching filter and says the values belong in no other |
| Period cohorts | offered as a population | not a population; nothing is carried |
| Untyped legacy cohort | applied as account ids | not guessed at |
| Truncated cohort | 200 of N frozen as "the previous result" | completeness travels with the cohort; a truncated one is disclosed and clarified, never frozen |
| Fresh question after `exclude 340B` | inherited the filter | told explicitly not to; verified `is_340b` returns to `include` |
| Ambiguous `"those"` | silently broadened or retained | asks |
| Inherited filters | silent | disclosed in the answer |

Migration `009` adds `cohort_complete` and `cohort_total`. A turn recorded
before the column reports **incomplete**, because unknown completeness is not
completeness.

Verified end to end on the full dataset: after `exclude 340B`, a fresh
revenue question returns `is_340b = include`; a bare `"those?"` clarifies; a
carried cohort discloses *"Still looking at the 3 accounts from your previous
question."*

`tests/unit/test_live_adapter_contract.py` — 20 tests against a **fake
transport** that records the real request, so the prompt and tool payload are
inspected rather than reimplemented. They also assert the plan schema exposes
no `sql`, `table`, `role`, `scope` or `credential` field, and that a tool call
under any other name is refused.

---

## Phase 1C — request-local planning results (complete)

`BedrockPlanner.last_usage` was instance state on a planner created once per
process lifespan, written by whichever call finished most recently and read
by the pipeline afterwards. Two consequences:

- **Concurrency.** Two requests in flight meant both reported the usage of
  whichever finished last.
- **Repair.** A repaired request overwrote the first attempt's usage, so the
  tokens it actually spent went unreported.

`PlanningResult` is now frozen and returned per call, carrying the plan, the
provider and model identity, the prompt and planner-contract versions, and a
record of **every** attempt with its own outcome and usage.

`TokenUsage.known` separates *"the provider reported nothing"* from *"it cost
nothing"*. A transport failure records **unknown** usage, not zero — the
request may have reached the provider and been billed. The offline planner
reports unknown for the same reason: no provider was called, so zero would be
a measurement nobody took.

The pipeline reads usage from the result of *this* request, attaches a
planning summary to `PipelineResult`, and sets `usage_known`,
`prompt_version`, `planner_attempts` and `planner_repaired` on the audit
record. The evaluator reads the same summary instead of planner state.

> **Correction, 30 September.** This section originally said those four
> fields were *written to the audit row*. They were set on the in-memory
> record and dropped at the INSERT, which named only the original 19
> columns — the same defect the review found for `intent_gaps`. Nothing
> checked that a key set on the record had a column. They are persisted
> since migration 011, and `tests/security/test_audit_contract.py` now
> fails if a key is set without one.

Verified: interleaved requests of 10 / 9,000 / 20 input tokens report exactly
those; a repair reports **220**, not 120; a failed repair names the 210
tokens it spent; partial usage is preserved; the result rejects mutation.

---

## Phase 1D — credential handling and evidence integrity (in progress)

### Connection strings (R8, A7)

`Settings.dsn()` built a URL by interpolation:

```
postgresql://{user}:{password}@{host}:{port}/{name}
```

`bootstrap_db.py` generates passwords, so a reserved character in one is not
a hypothetical input. `@` ends the userinfo section, which means the real
host became part of the password and the client connected somewhere else —
or, worse, somewhere that existed. `/` ends the authority, `?` starts a
query, `#` starts a fragment.

It now uses `psycopg.conninfo.make_conninfo`, the keyword/value builder the
driver itself consumes. There is no authority section for a credential to
break out of, and an empty password is omitted rather than sent as an empty
one — local peer authentication and a blank password are different things.

`tests/unit/test_dsn_encoding.py` covers ten reserved-character passwords.
It never prints or asserts a credential's text: it parses the DSN back with
`conninfo_to_dict` and asserts what the DSN *means* — that host, port,
database and user survive, that the password round-trips by equality, and
that `x' host='evil.example.com` and `x' sslmode='disable` cannot be
smuggled through a credential.

**Measured:** 37 pass on the fix; **17 of the 37 fail** against the previous
implementation.

### Evidence capture (A20)

Redaction was a pattern over variable *names*:
`PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|DSN|CONN|AUTH`. That is a denylist, and
it is only as good as the last name somebody thought of. Measured against six
credential-carrying names, **four walked straight through**:
`PAC_EVALUATOR_LOGIN`, `PGPASSFILE`, `BEDROCK_BEARER`, and any new name at
all.

Two mechanisms replace it, because they fail in different directions:

| Mechanism | Covers | Default |
|---|---|---|
| `ENV_VALUE_ALLOWLIST` | environment overrides — a name/value pair the recorder controls | **deny**; the value is recorded only for names that describe *what* ran |
| `SECRET_VALUE_PATTERNS` | everything else — summary lines, exceptions, command arguments, nested structures | strip credential-*shaped* text from every string at any depth |

A withheld variable is still recorded as present, because *that the run was
configured* is the fact a reviewer needs. The scrub runs over the assembled
record, so a field added later is covered without anyone remembering to
redact it, and over the terminal echo as well — a CI log is as durable as a
committed file and usually more widely read.

`tests/unit/test_evidence_redaction.py` plants a unique synthetic sentinel,
runs the real recorder, and greps the bytes that were written. Three shapes,
because they fail differently: an environment override, a connection string
in ordinary output (no name to match on, only a shape), and an exception
raised by the child (text the recorder never chose). Nested values are
covered directly against `_scrub`. One test guards the allowlist itself —
adding `PAC_DB_PASSWORD` to it would silently undo every other test.

**Measured:** 37 pass on the fix; **27 of the 37 fail** against the previous
implementation.

### A record that was quietly wrong (D4, A21)

Writing the above surfaced a defect in the recorder itself. `_git()` strips
its output before the caller splits it into lines, and `git status
--porcelain` begins every line with a two-character status field whose first
character is a space for an unstaged change. So the first path in every
record lost its first character.

Five records written before this commit carry it:

| Record | Recorded | Actual |
|---|---|---|
| `p1a-continuity.json` | `pp/conversation/state.py` | `app/conversation/state.py` |
| `p1a-defect-d2-fixed.json` | `pp/conversation/state.py` | `app/conversation/state.py` |
| `p1b-defect-d1-fixed.json` | `github/workflows/ci.yml` | `.github/workflows/ci.yml` |
| `p1b-strict-gate.json` | `github/workflows/ci.yml` | `.github/workflows/ci.yml` |
| `p1c-planning-result.json` | `pp/llm/planner.py` | `app/llm/planner.py` |

They are **left as written**. Re-recording them would stamp them with a SHA
and a worktree they did not measure, which is a larger inaccuracy than the
one being corrected (assumption 5). The three Phase 1D records were
re-recorded because the fix landed before they were final.

The parser now reads `--porcelain -z`, which also survives paths containing
spaces and does not misread a rename's origin field as a second entry.

### Documentation reconciled against measurement (A22)

Four documents disagreed about the size of the suite:

| Document | Stated | Actual |
|---|---:|---:|
| `README.md` | 368 | 517 |
| `DESIGN.md` | 148 | 517 |
| `docs/REQUIREMENTS.md` | 377 | 517 |
| `docs/EVALUATION.md` | 148 | 517 |

`docs/REQUIREMENTS.md` also credited `test_thresholds.py` with 17 tests; it
has 26. `DESIGN.md`'s four-layer table was stale in every row, and described
`evals/questions.yaml` as *held out* when it is the set the system was
developed against.

Nobody had lied. Each number was correct on the day it was typed, and there
was nothing to notice when it stopped being correct. That is the failure
mode worth fixing, not the individual numbers.

`docs/TEST_INVENTORY.md` is now the one measured source: counts per layer,
the command that produces each, the evidence record behind it, **what each
layer does not establish**, the three evaluation suites with their differing
standing, the state of both browser suites, and CI's history of accepting
skips. Every present-tense document points at it instead of restating a
number.

`tests/unit/test_documented_counts.py` (14 tests) holds it closed. It
collects the suite once and then requires:

- each inventory row to equal what pytest collects;
- the stated total to equal the sum of its parts;
- the inventory's security count to equal CI's `--min-tests` floor, so the
  document and the gate cannot drift apart;
- every count claim in `README.md`, `DESIGN.md` and `docs/REQUIREMENTS.md`
  to be either a per-file claim verified against that file, or a number some
  suite currently produces;
- each of those documents to point at the inventory;
- `docs/REMEDIATION.md` and `docs/EVALUATION.md` — dated logs whose numbers
  are deliberately **not** restated — to say so in their own text.

Verified by reverting `README.md` to its old 368 and the inventory's unit row
to 330: the guard names both, with the line number and the counts that do
exist.

### Separated results

| Layer | Result | Record |
|---|---|---|
| Unit | 344 passed | `p1d-suite-unit.json` |
| Integration | 58 passed | `p1d-suite-integration.json` |
| Security (strict gate) | 115 passed | `p1d-suite-security.json` |
| Total | 517 passed | `p1d-suite-total.json` |
| Model evaluation — regression | 38/38 offline | `p1d-eval-questions.json` |
| Model evaluation — held-out 1 | 12/12 offline | `p1d-eval-holdout.json` |
| Model evaluation — held-out 2 | 11/12 offline | `p1d-eval-holdout2.json` |
| Browser — component | 6 passed, ×3 | `p1d-suite-browser-component.json` |
| Browser — end to end | 6 tests, needs a served application | — |

### The browser suites (D5)

The component suite had not run from this checkout for weeks, and the
recorded reason was wrong. `docs/REMEDIATION.md` row 35 concluded *"the
stall was the filesystem, not the tests ... that is no longer a
hypothesis"*, on the strength of three runs from a fresh clone outside
`~/Desktop`. I repeated that reasoning at the start of this phase and
wrote the same conclusion into this document.

A fresh checkout also has a fresh cache, so every observation supporting
the filesystem theory supported the cache theory equally well. Nobody had
separated the two variables. Separating them:

| Where | Result |
|---|---|
| A plain Node worker under the repository path | replies in **11 ms** |
| The tree copied to `/private/tmp` | **6 passed, 503 ms** |
| The tree copied to `~/Desktop` but **outside this repository**, `node_modules` symlinked back into it | **6 passed, 578 ms** |
| The repository itself, after `rm -rf web/node_modules/.vite` | **6 passed, 509 ms**, then 456 / 376 / 375 ms |

The third row is the one that settles it: same filesystem, same folder
tree, same `node_modules` — and it passes. A stale entry in
`web/node_modules/.vite` was hanging the worker, which then timed out
after 60 s having collected nothing, under both the `forks` and the
`threads` pool.

`npm test` now clears that directory first. The suite takes under half a
second, which does not need a cache. The misdiagnosis is corrected in
`README.md`, `web/vitest.config.js` and — as a dated correction rather
than a rewrite — in `docs/REMEDIATION.md`.

Worth naming as a general failure: the evidence was consistent with the
conclusion, and the conclusion was still wrong, because the experiment
that would have discriminated between two explanations was never run. An
environmental diagnosis is a comfortable place to stop.

Playwright needs the application served and credentials in the
environment. It is not part of the offline gate and last passed against
the deployed instance on 2026-09-25.

---

## Before closing Phase 1

### The gate fails when a prerequisite is taken away (A24)

A gate that cannot fail is not a gate, and this one had been green without
being able to fail. `evidence/probes/gate_requires_its_databases.py` removes
the disposable authorization database — by name, never by dropping anything
— and runs the same module three ways:

| Run | Prerequisite | Gate | Exit | Time |
|---|---|---|---:|---:|
| 1 | removed | off | **0** | 30.4 s |
| 2 | removed | on | **1** | 30.3 s |
| 3 | present | on | **0** | 0.4 s |

Run 1 is what CI did for its entire history. Run 3 matters as much as run 2:
a gate that also fails when everything is present gets switched off.

Confirmed at full scope as well — with the authtest database absent,
`pytest tests/security -q` reports **64 passed, 51 skipped, exit 0**, and
the same run under `--release-gate --min-tests 115` turns those 51 skips
into errors and exits non-zero. With both databases present: **115 passed**,
no skips in any phase (`p1d-suite-security.json`).

Note the probe's exit semantics are the **opposite** of the Phase 0 defect
probes: those exit 0 while a defect is present, this one exits 0 when the
gate behaves correctly.

### Cohort completeness (A25, D6)

Three different limits can cut a cohort down, and the code treated them as
one:

| Limit | Meaning | Complete? |
|---|---|---|
| Ranking limit — "top 5" | those five *are* the population asked about | **yes** |
| Response cap — `max_result_rows` | rows past the cap are never shown | **no** |
| Storage cap — 200 ids per turn | a conversation row is not a result set | **no** |

Completeness was computed from the storage cap alone. Under the default
configuration the two caps mask the difference — 5,000 is larger than 200,
so any response truncation also exceeded the storage cap — but
`max_result_rows` is configuration. The same rule, at a configured cap of
50:

```
old:  complete=True   ids=51  total=51
new:  complete=False  ids=50  total=None
```

Three things wrong in that first line. The answer *was* truncated. `ids=51`
includes the extra probe row the compiler requests so the renderer can
detect truncation — a row nobody is ever shown, which could then have been
carried into a follow-up as part of "those". And `total=51` states the
truncated row count as though it were a population count, when the query
stopped looking; it is a floor, so it is now left unknown and reads *"50 of
more accounts (truncated)"*.

The rule now lives in one function, `summarise_cohort`, rather than being
computed inline in the pipeline where it could not be tested and could
drift from the renderer's own truncation check.

Legacy records were already right and are now covered by a test: a turn
written before migration 009 has NULL completeness, and NULL is read as
**incomplete** when the turn has a cohort, because unknown completeness is
not completeness.

`CONTINUITY_VERSION` 1.0.0 → 1.1.0.

---

## Phase 1 — closing record

Every change, the command that verified it, and the commit it landed in.
Each commit was verified on its own before the next was written.

| Commit | Change | Verifying command | Outcome |
|---|---|---|---|
| `73f0f7e` | Strict release gate rules on outcomes, not phases (D1, D3) | `pytest tests/unit/test_release_gate_strict.py -q` | 15 passed |
| `b72f89c` | Provider-independent continuity; typed cohorts reach the live prompt (D2) | `pytest tests/unit/test_live_adapter_contract.py -q` | passed |
| `f30c617` | Immutable request-local `PlanningResult`; usage survives repair (R2) | `pytest tests/unit/test_live_adapter_contract.py -q` | passed |
| `522bae6` | DSN via `make_conninfo` (R8, A7) | `pytest tests/unit/test_dsn_encoding.py -q` | 37 passed; 17 fail on the previous implementation |
| `522bae6` | Evidence capture: allowlist + value scrubbing (A20) | `pytest tests/unit/test_evidence_redaction.py -q` | 37 passed; 27 fail on the previous implementation |
| `522bae6` | Evidence records state the changed-file list accurately (D4, A21) | `pytest tests/unit/test_evidence_record_accuracy.py -q` | 10 passed |
| `927774a` | One measured source for test counts; drift guard (A22) | `pytest tests/unit/test_documented_counts.py -q` | 14 passed; both reverted numbers detected |
| `dbbe846` | Browser component suite runs from this checkout (D5, A23) | `cd web && npm test` | 6 passed, ×3, 375–509 ms |
| `91486f5` | The gate fails when a prerequisite is removed (A24) | `python3 evidence/probes/gate_requires_its_databases.py` | exit 0 — 0 / 1 / 0 as required |
| `91486f5` | Cohort completeness across three different limits (D6, A25) | `pytest tests/unit/test_cohort_completeness.py -q` | 16 passed |

Suite at the close of Phase 1: **533 passed**, 0 skipped, 0 xfail
(`p1d-suite-total.json`). Security under the strict gate: **115 passed**
(`p1d-suite-security.json`). Offline evaluation: 38/38, 12/12, 11/12.

### What Phase 1 does not establish

- **No live model was called.** Every figure here is offline or against a
  fake transport. The last live measurement was 2026-09-25 and is labelled
  with that date wherever it appears.
- **No deployment was touched.** The deployed instance still runs
  `7aae7cf`; nothing in this branch has been released.
- **The Playwright suite did not run.** It needs a served application and
  credentials, and is not part of the offline gate.
- **`max_result_rows` truncation was not exercised end to end.** The cohort
  rule is tested directly; no query was run against the 2M-row dataset that
  returns more than the default 5,000 groups.
- **The gate probe removes one prerequisite**, the authtest database. It
  does not exercise removal of the working database or the coherent
  fixture.
- **Original-data behaviour is untested** (R1, R4). That is Phase 2.

### Risks still open at the close of Phase 1

`R1` classification of original data · `R3` request-wide snapshot pinning ·
`R4` original-data onboarding · `R5` read→plan→answer concurrency ·
`R6` request-wide deadline · `R7` best-effort audit writes ·
`R9` observability. None were in Phase 1's scope; each has a numbered
phase.

---

## Deferred, and why

| Activity | Reason |
|---|---|
| Paid model inference | Constraint 1. Fake transports only. |
| AWS API calls, Terraform apply, deployment changes | Constraint 1. |
| External telemetry export | Constraint 1. Local collector profile only, off by default. |
| Database engine migration | Constraint 2. PostgreSQL retained. |
| Vector database, Kubernetes, Kafka, multi-agent roles | Constraint 6. No demonstrated requirement. |
| Multi-tenancy, enterprise SSO rollout | Interfaces only; external setup deferred. |
| Original-data acceptance | No authorized representative sample or reconciliation totals. |

---

## Assumptions recorded for this work

Routine local choices, recorded rather than asked.

1. `pharma_analytics` is the working database and is **never** a destructive test target. Destructive tests use `pharma_analytics_authtest`; hand-calculated fixtures use `pharma_analytics_fixture`.
2. Offline provider mode is the default for every gate. `fake` denotes a scripted transport with no network.
3. Evidence records are public; anything holding data or credentials is referenced, not inlined.
4. Contracts not yet versioned are `null`, never a placeholder string.
5. Historical measurements keep their original revision labels and are not restated.
