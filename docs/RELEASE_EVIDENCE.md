# Release evidence

## Current local candidate — 7 October 2026 (`7950e71`)

**Not production-ready.** The seven release defects are corrected and
regression-tested locally, and every local gate passes on the candidate
below. **Hosted CI passes**: run
[37651718276](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37651718276)
on `7bca4ca`, all four jobs, including an image built natively on
linux/amd64 (`sha256:dc31d26e33be…`). `7bca4ca` has `7950e71`'s application
code; the two runs before it failed on CI defects, now fixed: a trivy-action
tag that no longer exists, and too little shared memory for PostgreSQL in
its service container (also in `compose.yaml`). Details:
[REVIEW_2026_10_06.md](REVIEW_2026_10_06.md#hosted-ci-7-october). Nothing
has been published, deployed or sent to a paid model.

`7950e71` is `a05237b` (the candidate measured first, section below) plus
four changes, each ported from the parallel branch
`post-assessment/release-risks`, which corrected the same review findings
independently:

- **The evidence recorder counted results from anywhere in a run's output**
  (`cb8f00b`). A library's log line printed after pytest's summary ("pool.py:680
  error ... port 62692 failed") was recorded as 680 errors and 62,692
  failures. Counts now come from pytest's summary line alone. Every pytest
  record on this branch had parsed correctly; non-pytest commands now record
  `counts: null`.
- **Four log calls fell to `log.unclassified` or `log.external`** (`7526e69`):
  three telemetry debug messages and the ingestion job's telemetry warning.
  Nothing leaked, but the events lost their meaning. They are registered,
  and `tests/unit/test_log_registry.py` fails if any log call in `app/` or
  `scripts/` is not.
- **14 behavioural tests** (`a85c91b`): 7 log-leak cases with markers (an
  opaque token, a one-word entity, a preformatted error, a plain address, a
  third-party logger, uvicorn's access path, the collector address) and 7
  real-outage telemetry cases (closed port, silent server, held pool, locked
  table, an outage during a collection, a silent collector, a silent
  collector with no database). On the reviewed code `58b3d3e`: 12 failed and
  2 passed (`r5-port-reproduced.json`; the tree there was `58b3d3e` plus the
  added files, recorded by digest).
- **The connection budget counts the freshness pool** (`7950e71`): 25 per
  worker, 50 per two-worker replica.

| Identity | Value |
|---|---|
| Branch | `codex/release-defects-oct06` |
| Exact candidate measured | `7950e71304ec1c6da7985f6fa9ec41a2b154da6f` (tree `f9eaf48`) |
| Environment | macOS arm64; Python 3.13.2; PostgreSQL 16.14; Node 24.19.0 (official build, SHA-256 checked) for the frontend; offline planner |
| Databases | `pac_release` provisioned from scratch for this candidate (bootstrap, migrations, 2,000,000 sales); `pac_release_fixture`, `_authtest`, `_ingesttest` |
| Records | `r5-final-*.json`, each made with `--require-clean` on the candidate; the image record names the detached worktree's branch as `HEAD` |

| Suite / check | Pass | Fail | Skip | Evidence record |
|---|---:|---:|---:|---|
| Security boundary (`verify_runtime_role_safety`) | intact | — | — | `r5-final-boundary.json` |
| Full Python suite | 2078 | 0 | 0 | `r5-final-pytest.json` |
| Security release gate, strict floor 409 | 409 | 0 | 0 | `r5-final-security.json` |
| Ingestion release gate, strict floor 176 | 176 | 0 | 0 | `r5-final-ingestion.json` |
| The whole unit suite with no PostgreSQL reachable | 770 | 0 | 0 | `r5-final-unit-nodb.json` |
| The release gate's own tests | 19 | 0 | 0 | `r5-final-release-gate-selftest.json` |
| Components, Node 24, strict floor 27 | 27 | 0 | 0 | `r5-final-component.json`, `.detail.json` |
| Frontend production build, Node 24 | pass | — | — | `r5-final-web-build.json` |
| Browser journeys, Chromium, real local app | 10 | 0 | 0 | `r5-final-browser.json` |
| Offline regression questions (gate) | 38 | 0 | 0 | `r5-final-eval-questions.json`, `.detail.json` |
| Offline holdout 1 (spent) | 12 | 0 | 0 | `r5-final-eval-holdout.json`, `.detail.json` |
| Offline holdout 2 (spent; measurement) | 11 | 1 | 0 | `r5-final-eval-holdout2.json`, `.detail.json`. k-07 is the offline planner's known limitation |
| pip-audit 2.7.3 / production npm audit | pass | — | — | `r5-final-pip-audit.json`, `r5-final-npm-audit.json` |
| Gitleaks 8.30.1, full history | pass | — | — | `r5-final-gitleaks.json`; `.gitleaksignore` lists exactly two findings, the public CPython release key fingerprint in a historical trivy report |
| Image journeys, linux/amd64 | 25 | 0 | 0 | `r5-final-image-amd64.json` |

The image, built from the candidate with podman on an arm64 Mac under amd64
emulation: `linux/amd64`, 302 MB, revision label `7950e71304ec…`, image ID
`390a629596b886f42c2ed61039facffad8c552bc7493766b32fcbd18e0b4ec0e`, digest
`sha256:73494114edd00a0925c7ae7a53b6c4a149bfb198f5e646c663d6c1669a8f5afc`.
Trivy found no HIGH or CRITICAL vulnerability with a fix. It refused to
serve with no database and with the owner credential; provisioned a fresh
database; reported its release; signed in an Exec (priced answer) and a RAM
(no currency anywhere); ended a session on sign-out; wrote only sanitised
JSON log lines; and stopped cleanly on SIGTERM. It was not published, built
by hosted CI or run in staging.

Offline results measure deterministic compilation, authorization, execution
and rendering; 61/62 is not a live-model accuracy estimate. The full suite
includes 672 EXPLAIN compatibility cases, which do not establish numerical
correctness.

### Earlier candidate `a05237b` (measured first, 7 October)

**Not production-ready.** All seven requested release defects are implemented
and locally regression-tested. The known offline `holdout2/k-07` mismatch remains
a recorded failure. Nothing has been pushed, published, deployed, run on hosted
CI, or sent to a paid model during this work.

| Identity | Value |
|---|---|
| Branch | `codex/release-defects-oct06` |
| Exact candidate measured | `a05237b3e3621c6744c435e5007776adec3f46a1` |
| Starting tree | Clean `76298b6`; supplied review `58b3d3e` and final reviewed code `45db088` retained |
| Environment | macOS arm64; Python 3.13.2; local PostgreSQL 16.14; Node 24.19.0; deterministic offline planner |
| Dependencies | All 80 applicable Python packages match unchanged `requirements.lock`; frontend installed with `npm ci` from unchanged `package-lock.json` |
| Verification checkout | Detached temporary worktree of the same repository; each `r5-release-*` command record records the exact candidate SHA/tree and `worktree_clean: true` |
| Final documentation | The subsequent commit adds evidence and documentation only; it does not change the measured executable/configuration candidate |

[The finding-to-reproduction-to-fix ledger](REVIEW_2026_10_06.md) identifies
original observations, fix commits, regression files, failed attempts and
preserved contracts. Full test details and evaluation outcomes accompany the
command records in `evidence/runs/`.

| Suite / check | Environment | Pass | Fail | Skip | Evidence record |
|---|---|---:|---:|---:|---|
| Python unit | Local Python, part of full suite | 761 | 0 | 0 | `r5-release-full.json`, `.junit.xml` |
| Python integration | Disposable real PostgreSQL | 892 | 0 | 0 | `r5-release-full.json`, `.junit.xml` |
| Python security | Disposable real PostgreSQL | 409 | 0 | 0 | `r5-release-full.json`, `.junit.xml` |
| Full Python suite | Combined three rows above | 2062 | 0 | 0 | `r5-release-full.json` |
| Security release gate (overlaps full suite) | Real PostgreSQL, strict floor 409 | 409 | 0 | 0 | `r5-release-security.json` |
| Ingestion release gate (overlaps full suite) | Real PostgreSQL, strict floor 176 | 176 | 0 | 0 | `r5-release-ingestion.json` |
| Pure telemetry (overlaps unit suite) | PostgreSQL unavailable at port 1 | 18 | 0 | 0 | `r5-release-telemetry-unit.json` |
| Components | Node 24 / jsdom, strict floor 27 | 27 | 0 | 0 | `r5-release-components.json`, `.detail.json` |
| Browser journeys | Chromium, real local app / PostgreSQL | 10 | 0 | 0 | `r5-release-browser.json` |
| Offline regression questions | Existing synthetic fixture | 38 | 0 | 0 | `r5-release-eval-questions.json`, `.detail.json` |
| Offline holdout | Existing synthetic fixture | 12 | 0 | 0 | `r5-release-eval-holdout.json`, `.detail.json` |
| Offline holdout2 | Existing synthetic fixture | 11 | 1 | 0 | `r5-release-eval-holdout2.json`, `.detail.json` |
| Image journeys | Local linux/amd64 emulation with isolated PostgreSQL containers | 25 | 0 | 0 | `r5-release-image.json` |
| Frontend production build | Locked Node 24 install | pass | — | — | `r5-release-web-build.json` |
| pip-audit / production npm audit | Unchanged locks | pass | — | — | `r5-release-pip-audit.json`, `r5-release-npm-audit.json` |
| Gitleaks full history | Exact two public-fingerprint exceptions only | pass | — | — | `r5-release-gitleaks.json` |
| Trivy | Exact image; no HIGH/CRITICAL vulnerability with a fix | pass | — | — | `r5-release-image.json` |

The full Python run reports five existing pytest-asyncio deprecation warnings
from nested gate tests. No test was skipped or converted to xfail. Offline
results measure deterministic compilation/authorization/execution behavior;
61/62 is not a live-model accuracy estimate. The integration matrix includes
672 EXPLAIN compatibility cases; these do not establish numerical correctness.

Local image `pharma-analytics-copilot:smoke`, `linux/amd64`, 302 MB:

- Image ID: `5263cb9925936d9619beb0b10d9c1fff3791868fa90e2bc1dcd25b50e64017eb`
- Digest: `sha256:831164811ab69b33fb02ee980b78c3e8576cbfdf590299796e6327904803d072`
- Revision label: `a05237b3e3621c6744c435e5007776adec3f46a1`

This image was built and exercised with Podman on an arm64 Mac using amd64
emulation. It was not published, built by hosted CI or run in staging.

| Status | Established / outstanding |
|---|---|
| Implemented | Seven defect corrections, bounded metric labels, supported Node CI/image version, existing authorized-result charts, dashboard/alert specification, unchanged best-effort audit policy, corrected Exec WAC staging scenario, documented SSO disable / reviewed-image rollback |
| Locally verified | Exact clean candidate gates, image and dependency/secret scans above; original failures retained as `r5-attempt-*` |
| Staging verified | None for this candidate. Fake OIDC/provider and local collectors do not establish hosted integration |
| Production | Not ready; no deployment or capacity/accuracy claim |

External inputs still needed: authorization to push/run hosted CI and publish
or deploy; real OIDC registration; model access with agreed rates and explicit
spend caps; an independently authored fresh hash-frozen holdout for prompt
2.1.0; staging with multiple processes/replicas, real OTLP backend/alerting and
source feed contracts; agreed SLOs, retention, RTO/RPO and backup/PITR drills.
If every released answer must be durably audited, choose an atomic answer/run/audit
commit or fail-closed write/replay policy before implementation. Current audit
writes remain best effort. Follow [STAGING_VERIFICATION.md](STAGING_VERIFICATION.md).

## Historical evidence — 1–2 October 2026

The sections below describe the earlier `45db088` candidate. Their counts,
branches, image identities and observations are historical, not measurements
of the current candidate.

The single authoritative record of what this branch has established and
what it has not. Every claim points at a commit and at an evidence record in
`evidence/runs/` (schema: `evidence/schema.json`). A claim without one
belongs in the [Blocked](#blocked-what-needs-an-external-input) or
[Not established](#not-established) section.

| | |
|---|---|
| Branch | `post-assessment/release-risks`, which continues `post-assessment/production-readiness` |
| Code measured | `45db088` (every gate and the image, from a clean tree; each record was made with `--require-clean`) |
| Reviewed baselines | `de072e0`: the snapshot the 1 October 2026 review assessed, uploaded as a ZIP and a bundle. `c8aab5b`: the commit the 30 September review assessed; `main` still points at it. Nothing is merged or pushed |
| Versions | metric registry 1.4.0, policy 1.0.0, schema contract 1.0.0, prompt 2.1.0 (fingerprint `5ca5ddf08608fb64`), planner contract 2.0.0, graph 1.0.0, evidence schema 1.1.0 |
| Environment | macOS on Apple silicon (10 cores), Python 3.13.2, PostgreSQL 16.14, offline planner. The code is a clone outside iCloud-synced storage, with a venv built from `requirements.lock` with `--require-hashes`, as CI does. The image was built and run with podman 5.7.1 **for linux/amd64**, emulated with Rosetta in an arm64 VM |
| Databases | Provisioned from scratch for the final commit: `pac_release` (bootstrap, 22 migrations, 2,000,000 sales), plus `pac_release_fixture`, `_authtest` and `_ingesttest`. The fresh-provisioning tests and the image provision their own |
| Recorded | 2026-10-01 to 2026-10-02 |

## Verdict

**Not production-ready.** The code-level release conditions are met
locally, and this round's five findings are each reproduced, fixed and
pinned:

- **R1:** an SSO callback can no longer sign in a browser other than the one
  that started the sign-in.
- **R2:** every retry that does work counts against the per-user limits,
  across workers.
- **R3:** a live evaluation's spend is checked, before each call, against
  the request actually sent.
- **R4:** the ingestion job exports its own telemetry, and a stopped feed
  becomes visible without another batch.
- **R5:** malformed and non-finite input is refused or quarantined before
  SQL. A NaN price could previously be published.

Logs are now sanitised JSON with request correlation. Evidence records
name the exact bytes they ran on. The release image passes its journeys
**on linux/amd64**: refusals, fresh provisioning, Exec and RAM sign-in and
scope, sign-out, logs, a clean stop and a vulnerability scan.

The conditions that need an environment are not met, and this work could
not meet them:

- prompt 2.1.0 has never run against the live model;
- hosted CI has not run on this branch;
- the amd64 image was emulated on a developer machine, not built by CI;
- nothing is deployed or staged;
- real SSO, collector delivery and alerting are unverified;
- no SLO, RTO or RPO has been agreed.

[Blocked](#blocked-what-needs-an-external-input) gives the exact input and
command for each, and [STAGING_VERIFICATION.md](STAGING_VERIFICATION.md)
gives what staging must prove.

| Category | Meaning here |
|---|---|
| **Implemented** | In the code at `45db088` |
| **Locally verified** | An executable check passed on this machine, from a clean commit, with a record |
| **Staging verified** | Verified in a deployed environment. **Nothing is**: no staging exists for this branch |
| **Blocked** | Implemented as far as possible; verification needs an input listed below |

## Mandatory checks on the final code

Every row ran on `45db088` from a clean tree (`--require-clean`), against
databases provisioned from scratch. Records are in `evidence/runs/`.

| Check | Command | Result | Record |
|---|---|---|---|
| Security boundary | `verify_runtime_role_safety()` | intact | `r3-final-boundary.json` |
| Security (release gate) | `pytest tests/security -q --release-gate --min-tests 395` | **395 passed, gate satisfied** | `r3-final-security.json` |
| Ingestion (release gate) | `pytest` over the five ingestion files `--release-gate --min-tests 165` | **165 passed, gate satisfied** | `r3-final-ingestion.json` |
| Full pytest | `pytest tests -q` | **2,017 passed, 0 failed, 0 skipped** | `r3-final-pytest.json` |
| The release gate itself | `pytest tests/unit/test_release_gate_strict.py tests/unit/test_release_gate.py -q` | 19 passed | `r3-final-release-gate-selftest.json` |
| Component (vitest, gated) | `npm --prefix web test` with the JSON reporter, then `scripts/check_component_results.py ... 22` | **22 passed, none skipped, at the floor** | `r3-final-component.json` |
| Browser journeys (Playwright, Chromium) | `python3 scripts/browser_journeys.py` | **10 passed** | `r3-final-browser.json` |
| Python dependencies | `pip-audit==2.7.3 --require-hashes --disable-pip -r requirements.lock` | no known vulnerabilities | `r3-final-pip-audit.json` |
| JavaScript dependencies | `npm audit --omit=dev --audit-level=high` | 0 vulnerabilities | `r3-final-npm-audit.json` |
| Secrets in history | `gitleaks git . --redact` | no leaks in 133 commits | `r3-final-gitleaks.json` |
| **The image, linux/amd64** | `PAC_SMOKE_PLATFORM=linux/amd64 CONTAINER_CLI=podman scripts/image_smoke.sh` | **25 passed. Built from the commit in 80 s, 312 MB, labelled with it, platform `linux/amd64`. Trivy: no HIGH/CRITICAL with a fix (`r3-final-trivy-amd64.json`). Refuses to serve with no database or with the owner credential; provisions a fresh database; `/health` reports the release; not ready until data is published; an Exec and a RAM sign in and see their own scope; sign-out ends the session; server-generated `X-Request-ID`; every log line sanitised JSON; non-root; clean stop on SIGTERM. Image id `510b970d98fa…`, digest `sha256:d35069faac87…`** | `r3-final-image-amd64.json` (records the image id, digest, platform and revision label) |
| Offline evaluation: regression set (gate) | `run_evals.py --questions evals/questions.yaml` | 38/38 | `r3-final-eval-questions.json` |
| Offline evaluation: holdout 1 (spent) | same, `holdout.yaml` | 12/12 | `r3-final-eval-holdout.json` |
| Offline evaluation: holdout 2 (spent) | same, `holdout2.yaml` | 11/12. k-07 is the offline planner's known limitation: it reads "growing or declining month over month" as growth. Recorded as a failure, and a measurement in CI, not a gate | `r3-final-eval-holdout2.json` |

**Offline evaluation is not language accuracy.** It exercises the compiler,
authorization, execution and rendering with a deterministic planner. The
last live-model results are **historical**: 37/38, 11/12 and 11/12 on
2026-09-25, Claude Opus 4.5 on Bedrock, commit `e9a7e75`, with an
unversioned prompt that predates 2.1.0 ([EVALUATION.md](EVALUATION.md)).

## The 1 October review: every finding

Each finding was treated as a hypothesis, so a regression test was
committed **before** the fix as a strict expected failure. The record of
that test failing on unmodified code is the reproduction. The same tests
passing on the fix commit, with no expected-failure marker, is the fix.
Mutation checks then confirmed that each test notices the fix being undone.
The working ledger is [REVIEW_2026_10_01.md](REVIEW_2026_10_01.md).

| Finding | Reproduced on unmodified code | Fixed by | Tests now | Remaining limitation |
|---|---|---|---|---|
| **R1** OIDC login CSRF | `28299ec`: a browser with no cookies, given another browser's callback, received a session (`303`), and the start set no binding cookie. 4 tests (`r3-r1-reproduced.json`) | `ba91cd6`: each attempt is bound to a 256-bit secret in an HttpOnly `__Host-pac_oidc` cookie (Secure, `SameSite=Lax`, 10 min). Only its hash is stored (migration 020), and it is checked before the code exchange. Two tabs share a binding; a cancel ends the attempt only from its own browser | 31 in `test_oidc.py`, plus SSO on a database provisioned in one pass (`r3-r1-fixed.json`). A mutant that skips the binding check fails the 3 cross-browser tests | Tested against an in-process provider that checks PKCE, nonce, signatures and single use. A real IdP is a staging check ([RUNBOOK.md](RUNBOOK.md#enabling-single-sign-on)) |
| **R2** retries bypass user limits | `35ee659`: retries of failed, abandoned and cancelled runs were admitted uncounted: repeatedly, across conversations, past the concurrency limit, and by two racing workers. Deleting a conversation refunded its attempts. 9 tests (`r3-r2-reproduced.json`) | `52fa656`: every attempt that does work is admitted under the per-user lock and charged in `app_conv.run_attempts` (migration 021) in the transaction that admits it. Replaying a committed outcome is free. One committed outcome per key is unchanged | 11 in `test_retry_quotas.py` (`r3-r2-fixed.json`). Mutants: no enforcement on retry fails 7; counting runs instead of attempts fails 3 | Workers are threads with their own database sessions. Replicas are a staging check |
| **R3** evaluation cap exceedable | `0340ec1`: a 16,000 input cap admitted a 43 KB prompt and charged 17,000; the output reservation ignored `max_tokens` in both directions; unreported usage was charged 8,000 for a 43,400-byte prompt; 10M billed tokens did not stop the run. 5 tests (`r3-r3-reproduced.json`) | `c6986a9`: each call reserves the bound of the exact request it sends. Input is UTF-8/NFKC bytes plus a framing allowance; output is the configured `max_tokens`. A request that cannot fit is never sent. Usage above the reservation is a violation that stops the run | 28 in `test_eval_budget.py`, 24 of them on the budget and bound (`r3-r3-fixed.json`). Mutants: a fixed estimate fails 6; ignoring violations fails 1 | **The input bound is not a provider guarantee.** It assumes byte-level tokens and framing within the allowance, and is checked against reported usage on every call. About 24,100–24,200 per first call, roughly 5× the September cost |
| **R4** ingestion telemetry not wired | `eeac385`: the real ingestion command, with a collector configured, delivered nothing; three silent days after a healthy batch changed no exported value. 2 tests (`r3-r4-reproduced.json`) | `3fe964b`: the jobs command configures telemetry and flushes it in a bounded `finally` after publication. Freshness is read from the persisted watermark at every collection (`pac.ingest.since_success`, `pac.ingest.watermark_age`). `--check-freshness` exits 3 with stable codes. Missed-run, data-not-moving and absent-metric alerts are documented | 9 in `test_ingest_observability.py`, through the real command as a subprocess and a local OTLP receiver (`r3-r4-fixed.json`) | No real collector or alerting backend has received these. A hanging collector costs about 5 s; one not listening, about 2.6 s |
| **R5** input contract incomplete | `7bbf86d`: a NaN price was **published** into `sales`; `1e309` crashed publication; a string quantity raised `TypeError`; a bad timestamp crashed the adapter; a boolean version failed the batch; version 1.5 was stored as 2; NaN or NUL quarantine payloads failed their batch. 7 tests (`r3-r5-reproduced.json`) | `e512bbb`: a strict reader at the boundary. Envelope errors reject the batch whole with a stable code; record errors quarantine one event with a stable reason. Validation re-checks types, finiteness and bounds for any adapter. Reconciliation counts every record received. Quarantine payloads are made storable. `rejection_code` is recorded (migration 022) | 53 contract tests on PostgreSQL and 45 validation unit tests. The ingestion gate is 165 under `--release-gate` (`r3-r5-fixed.json`). Mutants: no finite check in validation fails 5; the reader passing NaN fails 2 | A real feed's contract and volume are untested. The sanity bounds (1,000,000 packs, $10,000,000 per pack) are judgement, not a specification |

The review's other items:

| Item | What was done | Evidence |
|---|---|---|
| Telemetry tests depend on ambient sampling | Reproduced: 7 failures at 1% sampling. Test providers now pin `ALWAYS_ON`; production still follows `OTEL_TRACES_SAMPLER` (`983b868`) | `r3-sampler-reproduced.json`; the suites pass at default and at 1% sampling |
| Logs unstructured, carrying exception text | Reproduced under uvicorn's own configuration and the real startup: a refused value, traceback text, and an OIDC callback's code and state with the client address (`5aa3b88`). Now one JSON line per record: the template, never the interpolated text; identifier-shaped arguments only; an exception's type, never its message; no query strings or addresses; a server-generated `X-Request-ID` on every line and response, plus the turn's `request_id` and `run_id` (`a9abb94`, `793e2ce`) | `r3-logs-reproduced.json`, `r3-logs-fixed.json`; the image test checks every line the app writes |
| Audit writes best effort | Not changed, because no stronger contract has been agreed. Documented as a decision with two replacements and their costs, and pinned by a test in which the database refuses the insert (`4337536`) | [OBSERVABILITY.md](OBSERVABILITY.md#audit-durability-the-current-policy-and-the-decision-it-needs) |
| Documentation states an old release identity | REQUIREMENTS, README and TEST_INVENTORY now label every historical result with its commit, model and prompt. Nothing was erased, and no regression set was relabelled as a holdout (`549660a`) | — |
| Records from uncommitted trees | Schema 1.1.0: every record names the tree hash, and a dirty tree's files by SHA-256. `--require-clean` refuses a release check on a dirty tree; `--image` records id, digest, platform and revision label (`72f9cc0`). Every final record below was made with `--require-clean` | `tests/unit/test_evidence_record_accuracy.py` |
| Live run reporting | Each run now reports p50/p95 latency, reported usage (unknowns counted, not zeroed) and cost at configured rates, beside correctness by category (`ccd2b4f`) | `tests/unit/test_eval_budget.py` |

Found while fixing, beyond the review's text:

- **Deleting a conversation refunded its share of the rate limit**, because
  the rate was counted from runs, which cascade away. Fixed with R2.
- **A fractional version was silently rounded**, changing an event's
  identity. Fixed with R5.
- **Ingestion stored raw exception text in `rejection_reason`**, which the
  serving role can read. It now records a stable code and the exception's
  type.
- **The first draft of migration 021 depended on a second migration
  pass.** 004's blanket grant added `UPDATE`.
  `tests/security/test_fresh_provisioning.py` caught it before commit.
- **`28299ec`'s commit message says plain pytest stays green with its
  expected failures.** The documented-count guard fails at that commit.
  The history is not rewritten; the correction is in the ledger.
- **`scripts/bootstrap_db.py --drop` defaults to dropping
  `pharma_analytics`** when `PAC_DB_NAME` is unset. Noted, not changed.
  This round's provisioning went through a guard that refuses that name.

## Earlier rounds

What follows was recorded on earlier commits, for the 30 September review
and the brief's phases. It still holds unless a section above says
otherwise. Counts and commits in it are the ones measured then.

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
| A live evaluation could overspend its budget. It checked once per question for two calls, while SDK retries allowed six; a retried call's usage was never counted | Inspection, then boundary tests | `6915a86` (every call metered; SDK retries off while metered) |
| A component suite that got smaller still passed CI: vitest exits 0 with an `it.skip` | Testing the gate both ways | `b422a56` (floor, no skips, tied to the files) |
| No admission control: requests queued without bound, and overload arrived as 5 s timeouts (5.2% at 32 clients) | The load profile | `01f2303` (measured in `r2-load-admission.json`) |

## The brief, phase by phase

### Phase 1: correctness and the release gate

The executable ledger above covers it, with Phase 1's own record in
[PRODUCTION_UPGRADE.md](PRODUCTION_UPGRADE.md).

Locally verified:

- CI runs the security suite under `--release-gate --min-tests 366`, plus
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
  release SHA. Migrations 010 to 022 add schemas, tables, columns, indexes
  and grants. 020's column is nullable, so the previous release's SSO
  inserts still work. An attempt that release starts is refused by this
  one, which fails closed during a rolling deploy. A test runs the previous
  release's insert statement against the new schema. The only replacements are on `app_conv.runs`, a table
  migration 012 itself created: its idempotency index is redefined (012),
  and its status `CHECK` is widened to allow `cancelled` (015). An older
  image does not depend on either, so it runs on the newer schema. This was
  checked by reading the migrations and by that one statement-level test,
  not by running an old image against a new schema.
- **Backup and restore:** [RUNBOOK.md §8](RUNBOOK.md). The procedure is
  drilled.
- **Retention:** `scripts/prune_state.py` in the jobs container
  ([RETENTION.md](RETENTION.md)).

## Reading the measurements

- **Capacity is offline-pipeline capacity.** 20 answers a second says
  nothing about live-model throughput, latency, rate limits or cost.
- **The 32-client timeout rate was a boundary**, and the limits are now
  set from it. With admission control, 32 clients had no timeouts, and the
  excess at 64 clients was refused in 82 ms with `Retry-After`. The limits
  are per worker, so a deployment's totals are worker limits × workers ×
  replicas ([CAPACITY.md](CAPACITY.md#admission-control)).
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

| Priority | Work | State |
|---|---|---|
| P0 | Hosted CI on the release commit: the test, frontend and supply-chain jobs, and the amd64 image build, scan and fresh-database journeys | **Ready.** The workflow runs on pushes to `post-assessment/**`. Needs authorization to push (below) |
| P0 | Live evaluation of prompt 2.1.0: a smoke run, the regression sets, then an independently written holdout frozen before use | **Runner ready**, with spend bounded per call (R3) and latency, usage and cost reported beside correctness. Needs credentials, an approved budget and the holdout questions (below) |
| P1 | Staging: real SSO, collector delivery, permission changes, retries across replicas, restart and checkpoint recovery, ingestion under load, stopped-feed alerts, and rollback and recovery | **Checklist ready** ([STAGING_VERIFICATION.md](STAGING_VERIFICATION.md)). Needs an environment, an IdP registration, a collector and authorization to deploy |
| P1 | Service and recovery targets: SLOs, RTO, RPO, retention. Then the redundancy and point-in-time recovery they require | **Needs decisions** from the product and data owners |
| P1 | Audit durability | **Needs a decision.** Best effort is the current policy, pinned by a test. The alternatives are fail-closed or one transaction ([OBSERVABILITY.md](OBSERVABILITY.md#audit-durability-the-current-policy-and-the-decision-it-needs)) |

## Blocked: what needs an external input

| Verification | Status | Exactly what is needed, and the command |
|---|---|---|
| Hosted CI | Defined; never run on this branch | Authorization to push. The current branch is `codex/release-defects-oct06`; its push procedure is in [RELEASE_HANDOFF.md](RELEASE_HANDOFF.md#pushing-when-authorized-not-done-here) (the command for `post-assessment/release-risks` that stood here is retired) |
| Live evaluation of prompt 2.1.0 | Implemented and budget-bounded; never run | AWS credentials with Bedrock access to `us.anthropic.claude-opus-4-5-20251101-v1:0`, the contracted per-million-token rates, and approved caps. The commands are below |
| A fresh, independent holdout | Not started | Questions and expected answers written by someone who has not seen the development sets, marked `status: "holdout"`, frozen with `scripts/freeze_holdout.py <file>`, and `evals/frozen.json` committed **before** the first run. Any written by this work would not be independent |
| Staging and deployment parity | Nothing deployed | A staging environment and authorization to deploy this branch's CI-built image by digest; then [STAGING_VERIFICATION.md](STAGING_VERIFICATION.md) |
| Single sign-on with a real IdP | Tested against an in-process provider only | An IdP client registration: issuer, client id and secret, and the redirect URI. Then [RUNBOOK.md](RUNBOOK.md#enabling-single-sign-on), "Verifying with a real provider" |
| Telemetry delivery, freshness gauges and alerts | Tested with in-memory exporters and a local OTLP receiver | An OTLP collector and a metrics backend: set `PAC_OTEL_ENDPOINT` in the app and jobs containers, and load the alert rules in [OBSERVABILITY.md](OBSERVABILITY.md#alerts) |
| Availability: replicas, managed PostgreSQL, point-in-time recovery | Prepared in docs; not provisioned | Approval for billable infrastructure, and the availability target |
| Service levels, RTO, RPO, retention periods | Proposals only | Decisions from the product and data owners |
| A real ingestion feed | Synthetic and JSON-file sources only | A source system, its batch contract and its schedule (which sets the missed-run threshold) |

**The live evaluation, in order.** Each step is a decision point, and each
run's record reports correct answers, refusals, unsupported requests, wrong
answers and failures separately, with p50 and p95 latency, reported usage
and cost ([EVALUATION.md](EVALUATION.md)):

```bash
export PAC_LLM_PROVIDER=bedrock          # with AWS credentials in the environment
export PAC_LLM_INPUT_USD_PER_MTOK=<rate> PAC_LLM_OUTPUT_USD_PER_MTOK=<rate>
# 1. Smoke: the first question of each family (16 checks).
python3 scripts/run_evals.py --provider bedrock --questions evals/questions.yaml --smoke \
    --max-input-tokens 150000 --max-output-tokens 70000
# 2. Regression sets (regression / spent: known behaviour, not unseen accuracy).
python3 scripts/run_evals.py --provider bedrock --questions evals/questions.yaml \
    --max-input-tokens 250000 --max-output-tokens 20000
python3 scripts/run_evals.py --provider bedrock --questions evals/holdout.yaml \
    --max-input-tokens 100000 --max-output-tokens 12000
python3 scripts/run_evals.py --provider bedrock --questions evals/holdout2.yaml \
    --max-input-tokens 100000 --max-output-tokens 12000
# 3. The independent holdout, after it is frozen and committed. Run it once.
python3 scripts/run_evals.py --provider bedrock --questions evals/<holdout>.yaml \
    --max-input-tokens <expected + 25000> --max-output-tokens <expected + 4096>
```

The caps allow the expected spend plus one reservation. Each call reserves
about 24,500 input tokens (the request's bound) and its `max_tokens`
(4,096) of output, but is charged what it bills. Adjust the later caps from
the smoke run's reported usage. For scale only: the September live runs
used 666,843 input / 19,521 output tokens for 124 questions, $4.20 at the
2026-10-07 price list's rate for the `us.` profile they used (the $3.82 this
line gave was the `global.` profile's rate; neither is the bill).

## Not established

- **Language accuracy under prompt 2.1.0.** Every accuracy figure in the
  docs is for earlier prompt text.
- **An exact token count before a call.** The evaluation budget's input
  bound is conservative under stated assumptions, and checked against
  reported usage. It is not the provider's count.
- **Anything about a real IdP, a real collector or a real feed.** Each is
  tested against a faithful local stand-in, not the real thing.
- **Behaviour across replicas.** Cross-worker limits and recovery are
  tested with threads and processes sharing one database, not with
  deployed replicas.
- **Behaviour under real user pacing and question mix**, and over a long
  soak.
- **Recovery into a new cluster** (roles first). It is documented, not
  drilled.
- **Anything about arbitrary datasets.** The system answers questions about
  this schema and contract. A different dataset needs a certified mapping.
