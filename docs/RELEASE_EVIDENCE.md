# Release evidence

The single authoritative record of what this branch has established and
what it has not. Every claim points at a commit and at an evidence record in
`evidence/runs/` (schema: `evidence/schema.json`). A claim without one
belongs in the [Blocked](#blocked-what-needs-an-external-input) or
[Not established](#not-established) section.

| | |
|---|---|
| Branch | `post-assessment/production-readiness` |
| Code measured | `7292f42` |
| Reviewed baseline | `c8aab5b`: the commit the 30 September 2026 review assessed. `main` still points at it; nothing on this branch is merged or pushed |
| Versions | metric registry 1.4.0 (`5a39a26aaa68db9e`), policy 1.0.0, schema contract 1.0.0, prompt 2.1.0, planner contract 2.0.0, graph 1.0.0 |
| Environment | macOS on Apple silicon (10 cores), Python 3.13.2, PostgreSQL 16.14, offline planner; the image built and run with podman 5.7.1 (arm64 VM) |
| Recorded | 2026-09-30 to 2026-10-01 |

## Verdict

**Not production-ready.** The code-level release conditions are met locally:

- all 20 review checks are fixed;
- every mandatory suite passes, with no skips;
- the security gate is strict.

The actual image has also been built from the commit and tested end to end
locally: 22 checks, a clean vulnerability scan, and an Exec and a RAM
journey on a freshly provisioned database. That test found a defect no
other test could: a first deployment could not sign anyone in. It is fixed
in `eebc974`.

The conditions that need an environment are not met. The current prompt
(2.1.0) has never been evaluated against the live model. Hosted CI has not
run on this branch. The image was built with podman on arm64, not by CI on
amd64. Nothing has been deployed or verified in staging. There is no
redundancy and no point-in-time recovery, and no service level, RTO or RPO
has been agreed. [Next steps](#next-steps-in-order) puts them in order, and
[Blocked](#blocked-what-needs-an-external-input) says what each one needs.

| Category | Meaning here |
|---|---|
| **Implemented** | In the code at `7292f42` |
| **Locally verified** | An executable check passed on this machine, with a record |
| **Staging verified** | Verified in a deployed environment. **Nothing is**: no staging exists for this branch |
| **Blocked** | Implemented as far as possible; verification needs an input listed below |

## Mandatory checks on the final code

| Check | Command | Result | Record |
|---|---|---|---|
| Full pytest | `pytest tests -q` | **1831 passed**, 0 failed, 0 skipped | `r2-image-pytest.json` |
| Unit / integration | `pytest tests/unit -q`, `tests/integration -q` | 652 / 815 passed | included in `r2-image-pytest.json` |
| Security (release gate) | `pytest tests/security -q --release-gate --min-tests 364` | **364 passed**, gate satisfied | `r2-final2-security.json` |
| Ingestion (release gate) | `pytest tests/integration/test_ingestion.py tests/unit/test_ingest_calendar.py --release-gate --min-tests 58` | 58 passed | `r2-final2-ingestion.json` |
| Component (vitest) | `npm ci && npx vitest run` on a clean copy of `web/` | **21 passed** | `r2-final2-component.json`. Run in place it collected nothing: iCloud had evicted the checkout's `node_modules` (see the record) |
| Browser journeys (Playwright, Chromium) | `python3 scripts/browser_journeys.py` | **10 passed** | `r2-final2-browser.json` |
| **The image** (podman, arm64) | `CONTAINER_CLI=podman scripts/image_smoke.sh` | **22 passed**, trivy: no HIGH/CRITICAL with a fix | `r2-image-smoke.json` |
| Review ledger | `python3 evidence/probes/review_2026_09_30.py` | **0 reproduced, 20 fixed, 0 open** | `r2-final2-ledger.json` |
| Offline evaluation: regression set (gate) | `run_evals.py --questions evals/questions.yaml` | 38/38 | `r2-eval-final-questions.json` |
| Offline evaluation: holdout 1 | same, `holdout.yaml` | 12/12 | `r2-eval-final-holdout.json` |
| Offline evaluation: holdout 2 | same, `holdout2.yaml` | **11/12**. k-07 is the offline planner's known limitation (it reads "growing or declining month over month" as growth), recorded as a failure | `r2-eval-final-holdout2.json` |

**Offline evaluation is not language accuracy.** It exercises the
compiler, authorization, execution and rendering with a deterministic
planner. The last live-model results (37/38, 11/12, 11/12 on 2026-09-25)
were measured on earlier prompt text, and are historical
([EVALUATION.md](EVALUATION.md)).

## The 30 September review: every finding

The baseline ledger (`r2-baseline-ledger.json`, commit `23edfa4`) found 16
reproduced, 3 already fixed by Phase 1, and 1 open with no probe yet. Each
check below runs the real function on the affected path.

| Finding | Check | Baseline | Fixed by | Final |
|---|---|---|---|---|
| F1a | Security job builds its database and runs the strict gate | already fixed | `73f0f7e`, `91486f5` (Phase 1) | FIXED |
| F1b | Frontend job runs the component suite | reproduced | `4971b58` | FIXED |
| F2a | Unknown product in any casing is blocked | reproduced | `e9ed69c` | FIXED |
| F2b | A known product dropped from the plan is caught | reproduced | `e9ed69c` | FIXED |
| F2c | A product the planner invented is caught | reproduced | `e9ed69c` | FIXED |
| F2d | Entity resolvers are on the real request path | reproduced | `e9ed69c` | FIXED |
| F3 | Live prompt types the cohort and classifies the turn | already fixed | `b72f89c` (Phase 1); the prompt's cohort text was refined in `ff97de7` | FIXED |
| F4a | A 500-account answer keeps all 500 for "those same accounts" | reproduced | `ff97de7` | FIXED |
| F4b | Repeated period rows are not new cohort members | reproduced | `ff97de7` | FIXED |
| F4c | A pending clarification and its choices survive | reproduced | `ff97de7`, `45c7d3c` | FIXED |
| F4d | Overlapping turns are ordered by revision or lease | reproduced | `ff97de7` | FIXED |
| F4e | A failed conversation write is reported | reproduced | `ff97de7` | FIXED |
| F5a | `facility_count_all` by territory or region executes | reproduced | `8e72694` | FIXED |
| F5b | "at least 100" includes 100 | reproduced | `03678fd` | FIXED |
| F5c | A sparse series does not average outside its window | reproduced (FA0017: 2.5, should be 0.667) | `5b4a56f` | FIXED (0.667) |
| F5d | A valid percentage metric is not called a count | reproduced | `03678fd` | FIXED |
| F6 | A refresh mid-request cannot pair one generation's plan with another's rows | open (no probe) | `873d699` | FIXED (`r2-snapshot-consistency.json`) |
| F7a | Planner usage is per call, not shared state | already fixed | `f30c617` (Phase 1) | FIXED |
| F7b | Intent gaps and reason codes reach the audit row | reproduced | `4971b58` | FIXED |
| F8 | A plan-only eval pass requires the request to have succeeded | reproduced | `4971b58` | FIXED |

## Defects found during this work, beyond the review

Each was found by a test or a measurement, fixed, and pinned by a test that
fails without the fix.

| Defect | Found by | Fixed by |
|---|---|---|
| Remembered model text (a plan's `interpretation`) was carried into the next prompt; `AI_SYSTEM_DESIGN.md` claimed no such channel existed | Prompt-injection tests with a persuaded planner | `0a19036` |
| The prompt changed three times under one `PROMPT_VERSION` | Fingerprinting the prompt | `0a19036` (2.1.0 and a fingerprint guard) |
| Unauthenticated `/ready` returned the raw database error: login role, host, "password authentication failed" | Failure-response tests | `d3ad12d` |
| A provider timeout triggered a false "repair" call and told the user to rephrase | Provider failure tests | `d3ad12d` |
| Pool exhaustion was reported as a slow query | Pool-exhaustion test | `d3ad12d` |
| Database outages were 500s, not 503s | Failure-response tests | `d3ad12d` |
| The scoped vocabulary was recomputed on every question (six `DISTINCT` scans) | Statement-timeout test | `d3ad12d` |
| "Top 20 facilities" was answered with one total, and nothing noticed | Choosing load-test questions on full data | `2fc0070` |
| After a publication under load, `VACUUM` reclaimed none of 2M replaced rows | Load profile | `66e62d2` |
| The anchor shift (`UPDATE` of every row) scrambled the table's period order and cut throughput by about 40% | Load profile | `66e62d2` |
| 13 known vulnerabilities (starlette 0.41.3, python-multipart 0.0.20) | `pip-audit` | `05ff022` |
| `pg_stat_user_tables` reported 0 dead rows while 20 were held | Reclaim test | `66e62d2` (counted from VACUUM's own report) |
| A freshly provisioned database could not sign anyone in: `login_attempts` was granted only on a second migration pass, and its sequence nowhere | Building and testing the actual image | `eebc974`, with a test that provisions in one pass |
| 11 HIGH vulnerabilities in the image, none of which pip-audit reported: Debian openssl and libpcre2, plus urllib3, msgpack and `pkg_resources` vendored in pip | trivy on the built image | `f81d67f` (security updates applied; pip removed from the runtime image) |

## The brief, phase by phase

### Phase 1: correctness and the release gate

The executable ledger above covers it, with Phase 1's own record in
[PRODUCTION_UPGRADE.md](PRODUCTION_UPGRADE.md).

Locally verified:

- CI runs the security suite under `--release-gate --min-tests 364`, plus
  the component tests and the browser journeys. A skip fails the gate:
  `r2-final2-security.json`, `tests/unit/test_release_gate.py`.
- Plan-only evaluation is scored apart from end-to-end success (`4971b58`).

### Phase 2 and 3: plan, entities, conversation, LangGraph

*Implemented, locally verified.*

- **Typed plan, compiler and validation preserved.** Unsupported
  combinations are refused by name, never in SQL. The compatibility matrix
  covers every metric × grain × filter family and is planned by PostgreSQL
  (`r2-compatibility-matrix.json`).
- **Entity mentions** are resolved under the caller's scope, with
  ambiguity offered as choices (`r2-entity-fidelity.json`).
- **Answer shape.** A dropped ranking or breakdown is disclosed; a reversed
  ranking is refused (`r2-answer-shape.json`).
- **Whole, typed cohorts** (500 of 500 kept); clarifications persisted.
- **Runs** have a lease, idempotency keys, and the conversation's revision
  checked atomically (`r2-conversation-reliability.json`).
- **The turn is a LangGraph 1.2.12 workflow** with PostgreSQL checkpoints:
  durable, resumable after restart, pruned when finished. Runtime context
  (identity) is never checkpointed (`r2-graph-durability.json`). Graph
  overhead: 55.3 ms median against 53.2 ms linear.

### Phase 4: identity, sessions, boundary, API

*Implemented, locally verified.*

- **Serving runs without the owner credential.** It is refused in the cloud
  environment, and migrations and ingestion run in a jobs container.
- **Sessions:** idle 30 min, rotation 15 min, fixed absolute expiry.
- **Request guard:** same origin, JSON only, 64 KiB, enforced while the
  body is read.
- **Per-user rate and concurrency limits** are counted in the database, so
  they hold across replicas. Cancellation is supported.
- **API v2** has a contract document guarded by a test.
- **OIDC single sign-on** (Authlib 1.8, joserfc), keyed by issuer and
  subject. Tested against a local in-process provider (`r2-oidc.json`). No
  real IdP: see Blocked.
- **Pricing cannot leak** through any shape a plan can take
  (`r2-suite-security.json`, `test_pricing_matrix.py`).
- **Feedback** (`97aab6e`) is owner- and access-scoped, with a redacted
  triage sample (`r2-feedback.json`).

### Phase 5: ingestion and consistent snapshots

*Implemented, locally verified.*

- **One generation per request.** A request reads the published generation
  and its facts in one repeatable-read snapshot, and refuses with `refresh`
  if a publication landed after planning. Publication uses `DELETE`, which
  is MVCC-safe, so readers are never blocked: 6.8 ms against 1,573 ms with
  the old `TRUNCATE` (`r2-snapshot-consistency.json`).
- **Incremental ingestion** ([INGESTION.md](INGESTION.md)):
  - a source adapter interface and a replayable synthetic feed;
  - reconciliation against declared control totals;
  - quarantine with a reason;
  - a ledger keyed by source identity and version: a replay changes
    nothing, a correction applies once, a tombstone holds, equal amounts
    stay distinct;
  - the business timezone decides the day;
  - calendar conventions are detected, never invented;
  - offsets shift in period order;
  - publication is in the same transaction as the facts, under one
    advisory lock;
  - watermarks and freshness are reported in `/api/me`.

  Records: `r2-ingestion.json`, `r2-final2-ingestion.json`,
  `r2-ingestion-scale-ordered.json`.
- **Attribution and access.** Historical attribution follows the current
  hierarchy, while access is always current
  ([INGESTION.md](INGESTION.md#attribution-versus-access)).

### Phase 6: observability, tool boundaries, retention

*Implemented, locally verified.*

- **OpenTelemetry traces and metrics** (`b910abd`, [OBSERVABILITY.md](OBSERVABILITY.md)):
  - every stage traced under the request;
  - token usage counted as reported, and unknown never treated as zero;
  - metric labels bounded, with no identifiers;
  - redaction at the call site and again in the exporter;
  - a down, stalled or raising collector never affects an answer.

  Record: `r2-telemetry.json`. No real collector or alerting backend: see
  Blocked.
- **Prompt injection.** A fully persuaded planner gains nothing: pricing,
  other territories, out-of-scope accounts, limits beyond the schema and
  remembered authority are all refused or bounded. The model has one
  forced tool that cannot express SQL (`r2-prompt-injection.json`).
- **Retention, export and deletion.** Owner-scoped export (bound to
  current access) and deletion across turns, cohorts, clarifications, runs
  and checkpoints. The audit trail is kept, and the serving role cannot
  delete it. Scheduled retention runs in the jobs container
  ([RETENTION.md](RETENTION.md), `r2-data-rights.json`).

### Phase 7: evaluation and resilience

*Implemented, locally verified, except where marked.*

- **Failure modes** (`r2-failure-modes.json`):
  - provider timeout, 429 and 529;
  - the request deadline;
  - a statement timeout;
  - pool exhaustion;
  - an unreachable database;
  - a failed checkpoint write (retry answers once);
  - concurrent pressure;
  - an outage returns 503 with nothing internal;
  - a real audit-write failure.
- **Load profile** ([CAPACITY.md](CAPACITY.md), `r2-load-profile.json`):
  peak 20.2 answers/s at 8 clients (p95 1.8 s). At 32 clients, p95 is
  4.0 s and 5.2% of answers are statement timeouts on the expensive class;
  nothing else fails. The scoped pool is the backpressure point. Run-to-run
  variance is 12–24 answers/s at 8 clients.
- **Refresh under load.** An in-week publication left reader latency
  unchanged and produced two correct `refresh` answers. A new-week
  publication took 434 s under load with reader p95 3.0 s.
- **Not done here: live-provider evaluation and a fresh holdout.**
  Blocked, see below.

### Phase 8: deployment, operations, documentation

*Implemented, locally verified, except where marked.*

- **Hashed lockfile** for every Python package, installed with
  `--require-hashes` by the image and CI. It installs into a clean
  environment and the unit suite passes there (`r2-lock-install.json`).
- **Scans.** `pip-audit` (after fixing 13 vulnerabilities), `npm audit`
  and `gitleaks` over all commits are clean (`r2-supply-chain.json`).
  `trivy` on the built image is clean after fixing 11 more
  (`r2-image-smoke.json`); [SUPPLY_CHAIN.md](SUPPLY_CHAIN.md) explains why
  both scanners run.
- **The image itself** is built from `git archive` of the commit and passes
  22 checks (`scripts/image_smoke.sh`): the CI image job's checks, plus
  readiness with data, an Exec and a RAM journey on a freshly provisioned
  database, sign-out, a clean stop, and the scan. It was built with podman
  on arm64, image ID `f8d727a3fdf6…`. That ID identifies this local build;
  the amd64 image CI builds will have its own.
- **Release identity** from the build to the image label, `/health` and
  spans. It is verified on the local image; CI also asserts it (not run).
- **CI runs on this branch** (pushes to `post-assessment/**`) and on
  manual dispatch, not only on `main` and pull requests.
- **Liveness and readiness are separate.** Readiness covers the database
  and the checkpoint store, not the model or the collector.
- **Graceful drain.** In-flight answers complete at SIGTERM; exit 4 s
  later (`r2-drain.json`).
- **Restore drill.** Restore-to-ready in 22.4 s for 2M rows, with counts,
  policies, grants, the boundary and identical answers all checked
  (`r2-restore-drill.json`).
- **Connection budget and multi-replica notes:**
  [RUNBOOK.md §9](RUNBOOK.md).

## Architecture and boundaries

```
browser ──HTTPS──▶ Caddy ──▶ app (uvicorn, 2 workers)           jobs container (owner role)
                              │ RequestGuard: same origin,        migrations, loads, ingestion,
                              │   JSON, size                      retention, restore drill
                              │ session cookie ─▶ Principal (role, scope, pricing)
                              │       read from `users` EVERY request; never from the
                              │       client, the model, memory, a document or a checkpoint
                              ▼
                       LangGraph turn: resolve ─▶ plan ─▶ check ─▶ answer
                         │                 │          │          │
                         │      model: one forced     │   compile (registry) ─▶ AST
                         │      tool, typed plan;     │   validate ─▶ PostgreSQL
                         │      sees data, never      │   (RR snapshot, generation check,
                         │      identity or SQL       │    RLS, column grants, timeouts)
                         │                            policy: role/scope/pricing
                         ▼                            refuses or narrows
          app_conv (typed plans, whole cohorts, clarifications, runs) · app_graph (checkpoints)
          app_meta.query_audit (hashes, codes; append-only for the app) · telemetry (redacted, lossy)
```

- **Identity.** The session cookie is the only credential the browser
  sends. Role, scope and pricing access are derived on the server, on every
  request, from `users`. Row-level security and column grants enforce them
  in the database, behind the policy. Each login role holds exactly the
  grants it needs, and serving never holds the owner credential.
- **Memory.** A conversation carries typed structure, not transcripts: the
  previous plan without model free text, whole typed cohorts, and pending
  clarifications. Nothing remembered is authority.
- **Data.** One published generation at a time. A request reads it in one
  snapshot. Ingestion publishes atomically under one lock and never blocks
  readers.

## Operating it

- **Configuration:** [RUNBOOK.md §3](RUNBOOK.md). Every setting is a
  `PAC_*` variable.
- **First deployment:**
  1. `scripts/bootstrap_db.py` (roles, superuser);
  2. `scripts/migrate.py` (jobs container);
  3. `scripts/load_data.py --mode full`;
  4. provision logins;
  5. start `app`.
- **Migrations** are additive and re-runnable. `migrate.py` applies them
  all and converges. There is no down-migration: recovery is forward, or a
  restore.
- **Ingestion:** `scripts/ingest.py batch.json`. A rejected batch changed
  nothing. Undo a published one by sending corrections, or by restoring and
  replaying ([INGESTION.md](INGESTION.md)).
- **Rollback of the application:** redeploy the previous image by its
  release SHA. Migrations 010 to 019 add schemas, tables, columns, indexes
  and grants. The only replacements are on `app_conv.runs`, a table
  migration 012 itself created: its idempotency index is redefined (012),
  and its status `CHECK` is widened to allow `cancelled` (015). An older
  image does not depend on either, so it runs on the newer schema. This was
  checked by reading the migrations, not by running an old image against a
  new schema.
- **Backup and restore:** [RUNBOOK.md §8](RUNBOOK.md). The procedure is
  drilled.
- **Retention:** `scripts/prune_state.py` in the jobs container
  ([RETENTION.md](RETENTION.md)).

## Reading the measurements

- **Capacity is offline-pipeline capacity.** 20 answers a second says
  nothing about live-model throughput, latency, rate limits or cost.
- **The 32-client timeout rate is a boundary**, not a pass or a fail. It
  should set per-replica concurrency and a global admission limit; no such
  limit is implemented ([CAPACITY.md](CAPACITY.md)).
- **Freshness** is source arrival delay plus batch schedule plus
  publication. Only publication is measured: about 40 s, or about 7 minutes
  for the first batch of a new week under load.
- **The 22 s restore** proves the procedure. It does not demonstrate
  point-in-time recovery, recovery from losing the host, or a production
  RTO ([RUNBOOK.md §8](RUNBOOK.md)).
- **Test counts** say how many checks exist, not what they establish. The
  layers, their assertions and what each does *not* establish are in
  [TEST_INVENTORY.md](TEST_INVENTORY.md).

## Next steps, in order

| # | Step | Evidence required | State |
|---|---|---|---|
| 1 | Build and test the actual image | Builds, starts, passes smoke tests, reviewed vulnerability scan | **Done locally** with podman on arm64 (`r2-image-smoke.json`). Still to do: the same on amd64 in CI |
| 2 | Run hosted CI on this branch | Every required suite executes with zero mandatory skips | The workflow now runs on `post-assessment/**` pushes and on manual dispatch. Needs this branch pushed |
| 3 | Evaluate prompt 2.1.0 against the live model | Model id, dataset, prompt fingerprint, results, failures, latency, tokens | The runner is ready: budget-capped, `--smoke` first, fingerprint recorded ([EVALUATION.md](EVALUATION.md#running-the-next-live-evaluation-prompt-210)). Needs credentials and a budget |
| 4 | Deploy the tested image to staging | Deployed digest = evaluated artifact; authenticated journeys pass | Needs a staging environment. The digest to compare is the one CI pushes, recorded with the run |
| 5 | Verify operational readiness | SSO, telemetry delivery, permission revocation, backup recovery, failure handling, agreed targets | Implemented and tested locally; each needs its environment or decision (below) |

Redundancy, managed PostgreSQL and point-in-time recovery follow from the
availability and data-loss targets, once those are set. They are not
prerequisites for staging. Telemetry delivery needs a collector; the
instrumentation itself is tested locally.

## Blocked: what needs an external input

| Verification | Status | Exactly what is needed |
|---|---|---|
| Live-model evaluation of prompt 2.1.0 (accuracy, latency, usage, cost) | Implemented, budget-capped; blocked | AWS credentials with Bedrock access to `us.anthropic.claude-opus-4-5-20251101-v1:0`, and an approved budget: about 75k input / 3k output tokens for the smoke run, then about 300k / 15k for the three regression sets |
| A fresh, independent holdout set | Not started | Questions written by someone who has not seen the system's development sets. One written by this work would not be independent |
| Hosted CI, including the supply-chain job and the amd64 image build and scan | Defined; never run. The workflow runs on this branch and on dispatch | Authorization to push this branch to GitHub |
| Staging and deployment parity | Nothing deployed | Access to a staging environment, and authorization to deploy this branch there |
| Single sign-on with a real IdP | Tested against an in-process provider only | An IdP registration: issuer, client id and secret, and the redirect URI registered |
| Telemetry export and the proposed alerts | Tested with in-memory exporters only | An OTLP collector endpoint and a metrics backend, to set `PAC_OTEL_ENDPOINT` and load the alert rules |
| Availability: more than one replica, managed PostgreSQL, point-in-time recovery | Prepared in docs; not provisioned | Approval for billable infrastructure, and an availability target |
| Agreed service levels, RTO, RPO and retention periods | Proposals only | Decisions from the product and data owners |
| A real ingestion feed | Synthetic and JSON-file sources only | A source system and its batch contract |

## Not established

- **Language accuracy under prompt 2.1.0.** Any accuracy figure in the
  docs is for earlier prompt text.
- **Behaviour under real user pacing and real question mix**, and over a
  long soak.
- **Recovery into a new cluster** (roles first). It is documented, not
  drilled.
- **Logs.** They are unstructured and not redacted the way spans are
  ([OBSERVABILITY.md](OBSERVABILITY.md#not-covered)).
- **Anything about arbitrary datasets.** The system answers questions
  about this schema and contract. A different dataset needs a certified
  mapping.
