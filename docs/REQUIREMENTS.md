# Requirement matrix

What the assignment asked for, what the code does, the evidence, and what is
still missing. Written against commit `e9a7e75`.

Evidence commands assume a loaded database; see [README](../README.md#tests).

---

## The five assignment requirements

| Requirement | Implemented behavior | Test / evidence | Remaining limitation |
|---|---|---|---|
| **1. Chat interface** | React 18 chat served from the same origin as the API. Multi-turn follow-ups patch a typed plan; two-stage loading states; a **New conversation** control; no SQL, schema or configuration exposed | `web/src/__tests__/` 6 jsdom tests; `web/e2e/` 6 Playwright tests in real Chromium, passing against the deployed URL | No streaming responses; no saved-conversation sidebar |
| **2. NL-to-SQL engine** | The model fills a typed `AnalyticalPlan`; the server compiles parameterised SQL, validates it against an AST allowlist, and executes it read-only with a statement timeout. Aggregations, rankings, thresholds, rolling averages, ratios, period comparisons, multi-table joins | 377 tests; `scripts/run_evals.py` 38/38 offline, 37/38 live | A per-period growth series is not expressible; the combination is refused by name |
| **3. Domain knowledge** | A versioned registry (`app/analytics/metrics.yaml`, v1.4.0) with 14 anchors into the supplied documents, injected into **every** planning request rather than retrieved | `tests/unit/test_registry_contract.py`; `tests/integration/test_coherent_fixture.py` computes each expected value by hand | Anchors are not automatically checked against the documents |
| **4. Security & access control** | PostgreSQL RLS for rows, column grants for `sales.wac`, separate login roles per privilege level. Scope and pricing re-read from `users` on every request | `tests/security/` **115 passing**, run under `--release-gate` so a skip fails the build | Single-tenant; no audit UI |
| **5. Cloud deployment** | AWS EC2, Docker Compose (app + PostgreSQL + Caddy), real Let's Encrypt HTTPS via `sslip.io`, Claude Opus 4.5 on Bedrock, 2,000,000 rows | `infra/smoke.sh` all pass; Playwright suite green against the live URL; `infra/terraform/` validates and plans | Single host, no redundancy; deployed latency under concurrency unmeasured |

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
| Prompt injection | Structurally impossible to reach a table, column, role or scope — the plan type has no field for any of them | eval `sec-05` | — |

---

## Analytical correctness

| Requirement | Implemented behavior | Test / evidence | Remaining limitation |
|---|---|---|---|
| PAP denominator | Each ratio declares `denominator_population`. PAP keeps the product population; market share widens to the surrounding market | `test_coherent_fixture.py` — ZENOVAX PAP is 20/130, market share still 0.40 | — |
| Product / NDC / strength filtering | All three are carried on the numerator and dropped only where the metric says to | `test_coherent_fixture.py`, `test_segment_share.py` | — |
| Facility counts | `facility_count` = facilities **with sales**; `facility_count_all` = structural, read from the hierarchy, still under RLS | `test_structural_counts.py` — 40,000 structural vs 25,561 with sales; RAM sees 2,648 | "Which health systems have the most facilities" still uses the sales-derived count |
| Period comparisons | A period grain inside a two-window comparison is **refused by name** with the two shapes that do work, rather than returning silent nulls | `test_specialty_and_periods.py` | Per-period growth series is not implemented |
| Limits after calculation | Thresholds and comparisons run before ranking and the row cap; comparison inputs are uncapped | `test_thresholds.py`, `test_comparison_grain.py` | — |
| Threshold questions | `Threshold` in the typed plan, compared against the plan's own metric in its own units | `test_thresholds.py` — 17 tests; oracle-verified | Only one threshold per query |
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
| Stale accuracy withdrawn and replaced | Re-measured live under the repaired judge: 37/38, 11/12, 11/12 | `evals/runs/*bedrock*`, `docs/EVALUATION.md` | No set is strictly held out with respect to the live model |
