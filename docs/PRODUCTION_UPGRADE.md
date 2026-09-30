# Production upgrade — working record

Post-assessment work toward supporting **original data** on the assignment
schema, with reliable execution, auditable evidence and safe operations.

This document is the current state. It does not describe intentions as
achievements: every claim points at a record under `evidence/runs/`, and
anything unmeasured says so.

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
| **Prompt** | **unversioned** |
| **Planner contract** | **unversioned** |
| **Graph** | **does not exist** |

The last three are recorded as explicit nulls in every evidence record rather
than omitted, so the gap stays visible until it is closed.

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
| **D1** | The release gate accepts a skip raised from a test body. `tests/release_gate.py` converts a skip to a failure only when `report.when == "setup"`. A `pytest.skip()` inside a test body skips at **call** phase, so `--release-gate --min-tests 1` exits **0** for a run that verified nothing. | `evidence/probes/gate_accepts_call_phase_skip.py` | `p0-defect-gate_accepts_call_phase_skip.json` |
| **D2** | The live prompt mistypes cohorts and mislabels turns. `build_system_prompt` emits *"The previous answer was about these **account ids**: ZENOVAX, GEMTARA"* for a **product** cohort, and *"This is a FOLLOW-UP"* whenever any previous plan exists. The typed-cohort work landed in `OfflinePlanner` and the pipeline but never reached the only text the live model sees. | `evidence/probes/live_prompt_mistypes_cohort.py` | `p0-defect-live_prompt_mistypes_cohort.json` |
| **D3** | CI does not run the strict gate. `.github/workflows/ci.yml:77` is `python -m pytest tests/security -q` with no `--release-gate` and no `--min-tests`. Confirmed by reading the workflow. | — | inspection |

### Source-level risks — the mechanism is in the code; production manifestation not reproduced here

| ID | Risk | Location |
|---|---|---|
| **R1** | Classification derives generic/biosimilar from **synthetic name suffixes** (`" GENERIC"`, `" BIOSIMILAR"`) and defaults every other non-company product to `branded_competitor`. On original data this silently manufactures a classification. Unknowns cannot currently stay unknown. | `app/data/classification.py` |
| **R2** | Shared mutable planner usage. `BedrockPlanner.last_usage` is instance state written per call and read later by `Pipeline`; one `Pipeline`/planner is created per process lifespan. Concurrent requests can read each other's usage, and plan repair overwrites first-attempt usage. | `app/llm/planner.py:229,303`, `app/pipeline.py:182`, `scripts/run_evals.py:493` |
| **R3** | No request-wide snapshot pin. The manifest, the vocabulary and the result rows are read in **separate transactions**; the vocabulary cache is keyed by `dataset_id` but reads current tables. A refresh between stages can label B's rows with A's calendar. Distinct from the already-fixed atomic publication. | `app/pipeline.py`, `app/analytics/entities.py` |
| **R4** | No original-data onboarding path. `load_data.py` accepts `seed`/`full` only, reads fixed generated paths, requires exact CSV headers, and loads while holding `TRUNCATE` locks. | `app/data/loader.py` |
| **R5** | Turn concurrency is protected at insertion, not across read→plan→answer. The advisory lock is taken inside `record_turn`, after planning. Two continuations can plan against the same stale parent. | `app/conversation/state.py` |
| **R6** | Per-statement timeout, no request-wide budget. Provider SDK retries plus plan repair can compound beyond any single limit. | `app/db.py`, `app/pipeline.py` |
| **R7** | Audit and conversation writes are best-effort — failures are logged and suppressed. | `app/pipeline.py` |
| **R8** | DSN built by raw string interpolation; a password containing `@`, `:`, `/` or `#` produces a malformed or misrouted URL. | `app/config.py:68` |
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
| **1** | Repair D1–D3, R2, R8; reconcile docs | next |
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
| A4 | Strict gate rejects setup skips, call skips, empty and narrowed selections | 1 | not run | |
| A5 | Live adapter prompt carries typed cohorts and explicit turn classification | 1 | not run | |
| A6 | Planner returns an immutable per-call result; usage survives repair | 1 | not run | |
| A7 | DSN tolerates reserved characters | 1 | not run | |
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
