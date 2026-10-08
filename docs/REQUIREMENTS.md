# Requirement matrix

What the assignment asked for, what the code does, the evidence, and what is
still missing. It describes the code on `codex/release-defects-oct06`. The
commit each current result was measured on is recorded in
[EVIDENCE_INDEX.md](EVIDENCE_INDEX.md) (earlier ones in
[RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md)), and counts are in
[TEST_INVENTORY.md](TEST_INVENTORY.md).

**Historical** marks a result measured on an earlier build: the submitted
assessment, or its September 2026 deployment. Each one names the commit,
prompt and model it was measured with, and none of them is a claim about
this branch. In particular, nothing on this branch has been deployed, and
prompt 2.1.0 has not been run against a live model.

Evidence commands assume a loaded database; see [README](../README.md#tests).

---

## The five assignment requirements

| Requirement | Implemented behavior | Test / evidence | Remaining limitation |
|---|---|---|---|
| **1. Chat interface** | React 18 chat served from the same origin as the API. Multi-turn follow-ups patch a typed plan; two-stage loading states; a **New conversation** control; no SQL, schema or configuration exposed | `web/src/__tests__/` jsdom component tests, gated in CI on a minimum count with no skips; `web/e2e/` Playwright journeys in real Chromium against a local server (`scripts/browser_journeys.py`, also in CI). Counts: [TEST_INVENTORY.md](TEST_INVENTORY.md) | No streaming responses; no saved-conversation sidebar |
| **2. NL-to-SQL engine** | The model fills a typed `AnalyticalPlan`; the server compiles parameterised SQL, validates it against an AST allowlist, and executes it read-only with a statement timeout. Aggregations, rankings, thresholds, rolling averages, ratios, period comparisons, multi-table joins | The pytest suite ([counts](TEST_INVENTORY.md)); `scripts/run_evals.py`: 38/38 with the offline planner on this branch. **Historical:** 37/38 live on 2026-09-25, Claude Opus 4.5 on Bedrock, commit `e9a7e75`, with an unversioned prompt that predates 2.1.0 | A per-period growth series is not expressible; the combination is refused by name |
| **3. Domain knowledge** | A versioned registry (`app/analytics/metrics.yaml`, v1.4.0) with 14 anchors into the supplied documents, injected into **every** planning request rather than retrieved | `tests/unit/test_registry_contract.py`; `tests/integration/test_coherent_fixture.py` computes each expected value by hand | Anchors are not automatically checked against the documents |
| **4. Security & access control** | PostgreSQL RLS for rows, column grants for `sales.wac`, separate login roles per privilege level. Scope and pricing re-read from `users` on every request | `tests/security/` passing under `--release-gate` ([counts](TEST_INVENTORY.md)), so a skip fails the build | Single-tenant; no audit UI. Audit writes are best effort, a policy that is pinned and awaits a decision ([OBSERVABILITY.md](OBSERVABILITY.md#audit-durability-the-current-policy-and-the-decision-it-needs)) |
| **5. Cloud deployment** | **Historical:** commit `7aae7cf` was deployed on 2026-09-25 to AWS EC2 with Docker Compose (app, PostgreSQL, Caddy), real Let's Encrypt HTTPS via `sslip.io`, Claude Opus 4.5 on Bedrock and 2,000,000 rows. This branch is not deployed. Its image is built and tested locally ([RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md)) | Historical: `infra/smoke.sh` all passed, and the Playwright suite was green against that live URL. `infra/terraform/` validates and plans | Single host, no redundancy; deployed latency under concurrency unmeasured. Staging for this branch needs an environment, an IdP registration and a collector |

---

## Security behaviours

| Requirement | Implemented behavior | Test / evidence | Remaining limitation |
|---|---|---|---|
| RAM sees one territory | RLS policy bound from a transaction-local GUC set by trusted server code | `test_access_control.py` | — |
| Director sees their region | Same policy, region scope | `test_access_control.py` | — |
| Exec sees everything | Global scope | `test_access_control.py` | — |
| WAC hidden from non-Execs | The scoped login role was **never granted** the column. Both `role == exec` and `can_view_wac` are required | `test_api_contract.py`, `test_access_control.py` | — |
| Revenue question from a non-Exec | Answered in volume with the substitution labelled, or refused with an alternative | `test_api_contract.py`; eval `sec-01`, `sec-02` | — |
| History cannot outlive entitlement | Open, list, load **and write** all require the recorded scope fingerprint to equal the principal's current one | `test_session_authorization.py` — 8 tests covering WAC loss, role change, territory move, another user | Rows are retained for audit, not deleted |
| Titles cannot leak | The whole row is withheld, not blanked — a redacted entry would still disclose that it exists | `test_session_authorization.py` | — |
| Identity switch in the browser | An identity epoch guards every async completion; in-flight requests are aborted and the transcript cleared on any auth change | `identity-isolation.test.jsx` — 6 tests, verified failing against `6c6d632` | — |
| Disabled credentials | `resolve()` joins credentials and refuses a disabled one; disabling and password rotation both revoke outstanding sessions | `test_session_lifecycle.py` | — |
| Login abuse | Rate limited per identity **and** per source over 15 minutes; HTTP 429, not 401 | `test_login_throttling.py` | No CAPTCHA or lockout escalation |
| SSO login CSRF | Each sign-in is bound to the browser that started it: an HttpOnly `__Host-` cookie, checked before the code is exchanged. A callback carried to another browser signs nobody in | `test_oidc.py`, reproduced first (`r3-r1-reproduced.json`) | Tested against an in-process provider. A real IdP is a staging check ([RUNBOOK.md](RUNBOOK.md#enabling-single-sign-on)) |
| Per-user limits | Every attempt that does work counts, retries included, under a per-user lock across workers. Replaying a committed answer is free | `test_retry_quotas.py`, `test_run_limits.py`, reproduced first (`r3-r2-reproduced.json`) | — |
| Prompt injection | Structurally impossible to reach a table, column, role or scope — the plan type has no field for any of them | eval `sec-05` | — |

---

## Analytical correctness

| Requirement | Implemented behavior | Test / evidence | Remaining limitation |
|---|---|---|---|
| PAP denominator | Each ratio declares `denominator_population`. PAP keeps the product population; market share widens to the surrounding market | `test_coherent_fixture.py` — ZENOVAX PAP is 20/130, market share still 0.40 | — |
| Product / NDC / strength filtering | All three are carried on the numerator and dropped only where the metric says to | `test_coherent_fixture.py`, `test_segment_share.py` | — |
| Facility counts | `facility_count` = facilities **with sales**; `facility_count_all` = structural, read from the hierarchy, still under RLS | `test_structural_counts.py` — 37,500 structural (`org_type = 'Facility'`) vs 25,561 with sales; RAM scoped | "Which health systems have the most facilities" still uses the sales-derived count |
| Period comparisons | A period grain inside a two-window comparison is **refused by name** with the two shapes that do work, rather than returning silent nulls | `test_specialty_and_periods.py` | Per-period growth series is not implemented |
| Limits after calculation | Thresholds and comparisons run before ranking and the row cap; comparison inputs are uncapped | `test_thresholds.py`, `test_comparison_grain.py` | — |
| Threshold questions | `Threshold` in the typed plan, compared against the plan's own metric in its own units | `test_thresholds.py` — 30 tests; oracle-verified | Only one threshold per query |
| Generic market share | `market_segment_share` — segment over surrounding market, with the derived classification disclosed | `test_segment_share.py`; eval `amb-01` = 0.700629 | — |
| 340B share | `share_340b` — the metric defines both sides, so it is correct whatever the planner sets | `test_segment_share.py`; eval `b340-01` = 0.122667 | — |
| Therapeutic areas | `products.specialty` is in the vocabulary; a named area the plan drops **blocks** | `test_specialty_and_periods.py` — oncology 368,411, not 484,394 | — |
| Unknown entities | Blocked with the token named and near matches offered; a planner-invented filter value is not treated as evidence the entity exists | `test_intent_fidelity.py` — 28 tests | — |
| Rolling averages | `Rolling` in the typed plan; a window function over the aggregated series, with the un-averaged point kept beside it | `test_thresholds.py`; hand check 22,523.00 = (22,314 + 28,362 + 16,893)/3 | Trailing only; no centred or weighted average |
| Declared grain | A duplicated group fails the request rather than rendering a misleading table | `test_declared_grain.py` | — |
| Zero denominators / nulls / empty | Null rather than zero or an error; impossible ratios reported with both components | `test_coherent_fixture.py` | — |

---

## Evaluation

| Requirement | Implemented behavior | Test / evidence | Remaining limitation |
|---|---|---|---|
| The judge can fail | 25 adversarial tests feed it deliberately wrong results | `test_eval_judge.py` | — |
| Status, metric and units checked | `nonempty` requires a named metric; `no_pricing` requires a real status and the SQL | `test_eval_judge.py` | — |
| Exact group identity and cardinality | Both directions — a group the oracle never produced fails | `test_eval_judge.py` | — |
| Cohort equality | A frozen cohort must equal the previous turn's rows | `test_eval_judge.py` | — |
| Pricing inspected everywhere | Headline, cells, notes, scope note and SQL | `test_eval_judge.py` | — |
| Expected and actual saved | Every run writes plan, SQL, answer, expected vs actual, latency, tokens | `evals/runs/` | — |
| Categories separated | Business answers / authorization refusals / unsupported / incorrect / failures | `scripts/run_evals.py` | — |
| No tuned set called held out | Three sets, each labelled | `evals/questions.yaml`, `holdout.yaml` (spent), `holdout2.yaml` | — |
| Stale accuracy withdrawn and replaced | **Historical:** re-measured live under the repaired judge on 2026-09-25 (Claude Opus 4.5, commit `e9a7e75`, unversioned prompt): 37/38, 11/12, 11/12. On `7e91f9f`: 37/38, 11/12, 10/12 | `evals/runs/reference-bedrock-full.json`, `docs/EVALUATION.md` | No set is strictly held out with respect to the live model. Prompt 2.1.0 is unmeasured live; the procedure and spend bounds are in `docs/EVALUATION.md` |
