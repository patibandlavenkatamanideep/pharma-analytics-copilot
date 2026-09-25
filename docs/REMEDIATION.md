# Remediation

Offline hardening phase. Starting commit **`6c6d632`** (93 files, 43 commits).

**No AWS, Bedrock, hosted model, hosted CI, deployment or billing action is
performed in this phase.** All verification is local, with
`PAC_LLM_PROVIDER=offline`, AWS credentials unset and
`AWS_EC2_METADATA_DISABLED=true`.

---

## Deadline

The candidate confirmed **Friday 25 September 2026, 4 pm "EST"**.

On that date US Eastern observes daylight time (UTC−4), so "EST" is ambiguous:
literal EST (UTC−5) would be one hour *later* than EDT. **The earlier reading —
16:00 EDT (20:00 UTC) — is treated as the working cutoff**, so the schedule
cannot overrun on either interpretation. Recorded, not inferred; no commit is
backdated.

---

## Baseline (recorded 2026-09-24 16:17 EDT)

| | |
|---|---|
| HEAD | `6c6d6327def9962f0cb24ab3c61c3a3225a2eb45`, working tree clean |
| Python / PostgreSQL / Node | 3.13.2 / 16.14 / 25.9.0 |
| Working database | `pharma_analytics` — dataset `full-182fd9082327`, mode `full`, 2,000,000 sales |
| Fixture database | `pharma_analytics_fixture` (coherent market) |
| Test databases | **none yet** — disposable ones are created per phase; the working database is never dropped or truncated |
| Test baseline | **148 passed**, 0 failed, 0 skipped (41 unit in 0.04 s; full suite 61 s) |
| Contract versions | metric `1.0.0` → **`1.1.0`** in phase 3 (PAP denominator semantics changed), policy `1.0.0`, schema contract `1.0.0`, mapping `1.0.0` |

### Defects reproduced against this exact HEAD, before any edit

### What 38/38 does and does not mean

A single number conflates four different outcomes, so the runner no longer
reports one. The current offline breakdown:

| | |
|---|---:|
| Correct business answers | **33** |
| Correct authorization refusals | **3** |
| Correct clarifications / unsupported requests | **2** |
| Incorrect answers | 0 |
| Execution failures | 0 |

**33/33 of the questions this system claims to be able to compute are
computed correctly.** The other five checks are right behaviour, not
capability: refusing an out-of-scope territory and declining an inexpressible
proportion both pass, and neither is evidence that a figure was calculated.
This must not be read as 100% question-answering accuracy.

`amb-01` is now a real answer rather than a disclosure. Generic share **is**
computable from the supplied data — generic market volume over total market
volume — so a metric was added (`market_segment_share`, registry 1.2.0) and
the expectation is a number from hand-written reference SQL, matching at
0.700629225451866. Answers disclose that generic-vs-branded-competitor is
derived, because `brand_flag = 0` covers both.

`b340-01` is still an unsupported request, counted as such. No metric expresses
"a filtered subset over the unfiltered whole" for 340B; the system says so
rather than answering in packs silently. The true value (12.27%) and its
reference SQL stay in the question file for whoever adds it.

### Scores restated under the repaired judge

| | Before (old judge) | After |
|---|---|---|
| Offline set | 38/38 | **36/38** |
| Live (Bedrock, 2026-09-24) | 37/38 | **withdrawn, not re-measured** — needs paid inference |

The live record is preserved as dated historical evidence. Re-judging it from
the stored file is only partly possible, because the runner did not record the
answer (R16); `b340-01`'s live plan was a two-row `is_340b` breakdown rather
than a percentage and would now fail, while `amb-01`'s live plan did carry
`classifications: [generic]` and cannot be rescored from what was saved.

**PAP denominator** (independent hand SQL vs the compiler, coherent fixture):

```
oracle    ZENOVAX paid packs        110
          ZENOVAX free packs         20
          all company paid+free     175
expected  20 / (110+20) = 0.153846153846
actual    20 / 175      = 0.114285714286   <- compiler drops the product filter
```

**Evaluator false positives** (offline planner, full dataset):

| Case | Asked for | Returned | Why it passed |
|---|---|---|---|
| `b340-01` | "what **percentage** of our volume comes from 340B accounts" | `paid_pack_units` = **59,419 packs** | `expect: nonempty` — any row passes |
| `amb-01` | "**generic** share in Platinum Compounds" | `brand_market_share` = **71.79%** (company brand) | `any_of [..., answered_with_note]`, satisfied by the unrelated boilerplate incomplete-period note |

Both are recorded here as *reproduced*, not fixed.

---

## Findings

Status values: **fixed** (with evidence), **already fixed** (closed with
evidence), **remaining**, **blocked**.

| ID | Finding | Phase | Status | Evidence | Commit |
|---|---|---|---|---|---|
| R01 | History/list/titles authorized only by owner, not by current scope or pricing permission; stored headlines can carry WAC amounts and old-territory labels | 1 | **fixed** | `tests/security/test_session_authorization.py` — 8 tests; 4 fail on `f8d6d31` | `0d83a29` |
| R02 | `resolve()` ignores disabled credentials; cookie name inconsistently applied; rotation/revocation undefined; startup privilege checks test fixed role names rather than effective grants | 1 | **fixed** | `test_session_lifecycle.py` (6; 5 fail before) · `test_privilege_boundary.py` (6; 4 fail before) | `0d83a29` |
| R03 | A delayed response from a previous identity can update the UI after an account switch | 1 | **fixed** | `web/src/__tests__/identity-isolation.test.jsx` — 4 tests pass against the fixed component. The before/after comparison is **not** recorded: see the note below. | `0d83a29` |
| R04 | Evaluation judge accepts semantic false positives; tuned set described as held out; release command tolerates skips and empty selections | 2 | **fixed** | `tests/unit/test_eval_judge.py` (18 adversarial cases; **12 pass the old judge**) · `tests/unit/test_release_gate.py` (4) | `244d5d0` |
| R05 | `_compile_ratio()` strips the product population from every denominator, so PAP proportion is wrong | 3 | **fixed** | `test_coherent_fixture.py` — PAP is 20/130, market share still 0.40; `test_registry_contract.py` (7) | `4c005ba` |
| R06 | Declared grain not enforced (duplicate groups); display limits applied before comparison ranking; label used as identity | 3 | **fixed** | `test_comparison_grain.py` (8) · `test_declared_grain.py` (8) | `4c005ba` |
| R07 | Unconditional "competitor-only" market warning; invalid conversion factors yield partial sums presented as totals | 3 | **fixed** | `test_quality_warnings.py` (7; all fail before) | `4c005ba` |
| R08 | Unresolved entities silently broaden the query; percentage/threshold/generic-share intents silently substituted | 4 | **fixed** | `app/analytics/intent.py`; `test_intent_fidelity.py` (25) | `9312ee5` |
| R09 | Follow-up vs fresh question not classified; cohort contents untyped; concurrent turns silently dropped | 4 | **fixed** | `test_cohort_typing.py` (12) · `test_turn_concurrency.py` (5); migration `007` | `9312ee5` |
| R10 | Rows and manifest publish in separate transactions; vocabulary cache not bound to dataset version; repeated seed loads collide on identity | 5 | **fixed** | `test_snapshot_publication.py` (5; all fail before) | `a4fdb59` |
| R11 | Failure/audit contract untested over HTTP; result-byte limit unimplemented; packaging omits `metrics.yaml`; UI lacks offline/new-conversation controls | 6 | **fixed** | `test_audit_contract.py` (7) · `test_packaging_and_limits.py` (12; 11 fail before) · `test_login_throttling.py` (8) | `5f09611` |
| R12 | Documentation contradicts itself on deployment, token measurement, run records and benchmark scope; demo narration overstates behaviour | 7 | **fixed** | See *Documentation reconciled* below | _phase 7_ |
| R13 | A 500 from `/api/ask` carried no request id, so a user's report could not be matched to the log line | 1 | **fixed** | `test_api_contract.py::test_a_planner_failure_is_reported_without_internals` | `0d83a29` |
| R14 | The serving process used the **owner** connection on every `ask()` to read the dataset manifest, so the API had to hold owner credentials | 1 | **fixed** | `test_privilege_boundary.py::test_serving_a_request_never_opens_the_owner_connection` | `0d83a29` |
| R16 | Run records stored the plan and SQL but not the answer, so a stored run cannot be re-judged after the judge changes — `amb-01`'s live result could not be rescored | 2 | **fixed** | `scripts/run_evals.py` now records headline, notes, warnings and rows | `244d5d0` |
| R15 | Dev-only npm advisories (vite dev server, vitest API server): 1 critical, 1 high, 3 moderate. `npm audit --omit=dev` is clean, so nothing ships in `dist/`. No semver-compatible fix exists; clearing them needs vite 6→8 + vitest 2→5, a build-toolchain migration | 6 | **accepted, recorded** | `npm audit --omit=dev` → 0 vulnerabilities | |

---

## Verification ledger

Every row records the command, the environment, the fixture, and **what the
result proves** — which is deliberately narrower than "it passed".

| # | Command | Env | Dataset / fixture | Result | Proves |
|---|---|---|---|---|---|
| 0 | `pytest tests -q` | offline, no AWS creds | `full-182fd9082327` | 148 passed | Baseline before edits. Does **not** prove authorization under permission change, nor evaluator soundness. |
| 1 | hand SQL vs compiler, PAP | offline | coherent fixture | 20/175 ≠ 20/130 | R05 reproduced. |
| 2 | pipeline, b340-01 / amb-01 | offline | `full-182fd9082327` | packs / company-brand share | R04 reproduced. |
| 3 | `pytest tests/security/test_session_authorization.py` | offline, no AWS creds | `pharma_analytics_authtest` (disposable) | 8 passed | Losing WAC, changing role, moving territory and being a different user each make prior history, titles and writes inaccessible; an unchanged user keeps theirs. Does **not** prove anything about an administrator or audit path, which is deliberately unchanged. |
| 4 | same file, `git stash` of the four app files | offline | same | **4 failed**, 4 passed | The four permission-change scenarios genuinely fail on `f8d6d31`. The other 4 were already correct and are regression guards, not claimed fixes. |
| 5 | `pytest tests/security/test_session_lifecycle.py` | offline | disposable | 6 passed / **5 fail** pre-fix | Disabling kills issued sessions; `resolve()` itself refuses a disabled credential; rotation revokes; a non-default cookie name is honoured by login, resolution and logout, and the default name is not accepted in its place. |
| 6 | `pytest tests/security/test_privilege_boundary.py` | offline | disposable + throwaway `pactest_*` roles | 6 passed / **4 fail** pre-fix | The startup check follows the configured roles, resolves BYPASSRLS reached through membership, and treats a missing role as a problem. Also: the API opens no owner connection. |
| 7 | `pytest tests/security/test_api_contract.py` | offline | disposable | 12 passed | SQL and plan are withheld unless asked for; a RAM's SQL cannot name `wac`; denied/clarify/error each carry a request id and no internals. |
| 8 | `npx vitest run` (`web/`) | **jsdom component tests**, not a real browser | stubbed fetch | 4 passed (122 ms of test time, 240 s of collection) | A response issued to one identity cannot reach the next one's screen, and signing in clears the transcript and conversation id. |
| 8b | same, against the pre-fix component | jsdom | stubbed fetch | **obtained on a second attempt — see rows 35–37** | Four attempts from the `~/Desktop` checkout stalled; the work was redone from a fresh clone outside it. |
| 9 | `npm run build` | — | — | built in 15.25 s | The production bundle still builds after the test tooling was added. |
| 10 | `pytest tests/unit/test_eval_judge.py` | offline | fakes only | 18 passed / **12 fail** against the pre-repair judge | The judge now rejects: a boilerplate note standing in for a qualifying one; a packs answer to a percentage question; `warning` with no needle; an empty oracle matching an empty answer; groups the oracle never produced; `no_pricing` judged without ever seeing the SQL; currency in the headline; a typo'd expectation type. |
| 11 | `python3 scripts/run_evals.py` (offline) | offline | `full-182fd9082327` | **36/38**, 2 failed | Under the repaired judge, on the same dataset that previously reported 38/38. The two failures are `b340-01` and `amb-01` — the cases the old rules were hiding. |
| 12 | `pytest tests/security -q --release-gate --min-tests 95` | offline | working + disposable | 95 passed | The gate now fails on a skip or a short collection. Previously `pytest tests/security -q` exited 0 with every test skipped. |
| 13 | `pytest tests -q` | offline | working + disposable | **202 passed** | 148 at baseline, 180 after phase 1, 202 after phase 2. |
| 14 | `pytest tests/integration/test_coherent_fixture.py` | offline | coherent fixture | 17 passed | PAP share of ZENOVAX is 20/130 = 0.1538, not 20/175 = 0.1143. Market share stays 0.40, so the widening that is correct for market share survived. Both fail before the fix. |
| 15 | compile `share_trend_pp` with `max_rows=2` | offline | n/a (SQL structure) | no `LIMIT` inside the CTEs | Each period is no longer truncated before the change is computed. Before: `LIMIT 3` in both CTEs, so "which product gained the most share" was computed from two independently truncated top-3 lists. |
| 16 | `pytest tests/unit/test_quality_warnings.py` | offline | fakes | 7 passed / 7 fail before | The competitor-only warning is silent when the dataset does report company rows in market data — which the coherent fixture does, so that answer previously carried a warning contradicted by its own data. |
| 17 | `pytest tests -q` | offline | working + disposable | **235 passed** | After phase 3. |
| 18 | `python3 scripts/run_evals.py` | offline | `full-182fd9082327` | 36/38 | Unchanged by phase 3: the two failures are still `b340-01` and `amb-01`. |
| 19 | pipeline, unresolved entity | offline | `full-182fd9082327` | `clarify`, was 484,394 packs | "What is the volume for FLOOBERTAX this quarter?" returned the whole company's volume presented as that product's. It now names the token it could not resolve and suggests near matches instead of answering a broader question. |
| 20 | `pytest tests/unit/test_intent_fidelity.py` | offline | fakes | 25 passed | Unresolved product and place block; proportion, threshold and dropped-classification gaps are disclosed rather than blocking, because the number returned is true but is not the whole question. |
| 21 | planner, product cohort on a follow-up | offline | fakes | `product_names`, was `account_ids` | A cohort of drug names was being applied as organization ids. |
| 22 | 6 concurrent `record_turn` on one conversation | offline | disposable | 6 of 6 kept | Was: `ON CONFLICT (conversation_id, seq) DO NOTHING` discarded the loser silently — the user saw an answer and the conversation had no record of the question. |
| 23 | `pytest tests -q` | offline | working + disposable | **277 passed** | After phase 4. |
| 24 | `python3 scripts/run_evals.py` | offline | `full-182fd9082327` | **38/38** | See the note below: this is not the old 38/38. |
| 25 | three consecutive `load("seed")` | offline | disposable | 3 distinct dataset ids | **Any** reload previously died on `users_pkey`: the seed file inserts users, business tables are truncated on reload and `users` deliberately is not. The system could not be reloaded at all. |
| 26 | injected failure in `_validate` during a load | offline | disposable | previous snapshot still published, tables non-empty | A failed load leaves the last good answer serving rather than a half-loaded one, and does not supersede it. |
| 27 | manifest vs live rows after a reload | offline | disposable | ids, counts and anchor agree | Rows and manifest now commit together. Before, the new rows were briefly live under the previous snapshot's reporting anchor — so "this quarter" was resolved against the wrong calendar. |
| 28 | `pytest tests -q` | offline | working + disposable | **282 passed** | After phase 5. |
| 29 | `python -m build --wheel`, inspect contents | offline | n/a | `metrics.yaml` present; before: **no non-`.py` file at all** | The registry is read at import, so the wheel could not start. |
| 30 | `render(...)` with 2,000 wide rows | offline | synthetic | table ≤ the byte limit, `truncated` set | `max_result_bytes` was configured and read by nothing. A row cap is not a size cap. |
| 31 | `pytest tests/security/test_login_throttling.py` | offline | disposable | 8 passed | A password could be guessed without limit and without trace. Now limited per identity **and** per source, 429 not 401, no password or clear IP in the log, and a refusal is not itself counted so an attacker cannot hold a user out indefinitely. |
| 32 | `pytest tests/security/test_audit_contract.py` | offline | disposable | 7 passed | Answered, denied and clarified requests all leave a row; it carries hashes, counts, timings and identity, and never the question, the SQL, the headline or a currency amount. |
| 33 | `npx vitest run` after the UI change | jsdom | stubbed fetch | **not obtained** | Same stall as ledger row 8b. `npm run build` succeeds; the New-conversation control and its test are committed unverified-this-run, and the limitation is listed in the README's open gaps. |
| 34 | `pytest tests -q` | offline | working + disposable | **309 passed** | After phase 6. Release gate 115 passed. |
| 35 | `npx vitest run` ×3 from `~/pac-uitest` (a fresh `git clone`, **outside `~/Desktop`**) | jsdom | stubbed fetch | 6 passed each, **479–769 ms per run** | The stall was the filesystem, not the tests. Collection: **59–108 ms** here against **239,780 ms** on `~/Desktop`. That is no longer a hypothesis. |
| 36 | same fresh checkout, `App.jsx` from `6c6d632` | jsdom | stubbed fetch | **6 failed** | The before/after that could not be obtained earlier. |
| 37 | isolated probe of the mechanism | jsdom | stubbed fetch | pre-fix: still on the login screen after a successful sign-in. Fixed: signed in | The race is worse than first described. `/api/me` issued while signed out rejects **after** `setUser(user)`, and its unguarded `.catch(() => setUser(null))` throws the user back to the login screen. That is why all six failed, and it now has its own named test. |
| 38 | hand SQL vs compiler, generic share | offline | `full-182fd9082327` | 0.700629225451866 both sides | `market_segment_share` computes the figure `amb-01` asks for. Its expectation is now a number from an independent oracle, not a note. |
| 39 | `python3 scripts/run_evals.py` | offline | `full-182fd9082327` | 38/38, **33 answers / 3 refusals / 2 unsupported / 0 wrong / 0 failures** | The runner now reports these separately. "38/38" alone conflated question-answering with correctly declining. |

---

## Scope explicitly deferred to a later phase

Recorded so nothing is silently dropped:

- Bedrock/provider adapter integration testing (no hosted model calls here).
- Deployed acceptance tests and external availability verification.
- Container/Compose execution and persistence checks.
- Hosted CI runs.
- Any account, model-access, cost or billing review.

**Known local tooling limitation.** `npx vitest run` completes only on its
first invocation on this machine; later runs block at 0% CPU in collection.
This is environmental (`~/Desktop`), not a property of the tests, and it means
the browser suite cannot currently be relied on as a repeatable gate here. It
is listed for the next phase rather than papered over.

The existing deployment record and `evals/runs/reference-bedrock-full.json`
(37/38) are **preserved as dated historical evidence**. The reviewer did not
contact the deployment, and neither does this phase; current availability is
therefore unverified rather than disproved.

---

## Documentation reconciled (phase 7)

| Contradiction | Was | Now |
|---|---|---|
| Token measurement | `docs/EVALUATION.md` said "token usage measured — 526,722 in / 18,572 out" and, **two lines later**, "Model token usage and spend: not measured" | One statement: usage measured and recorded per question in `evals/runs/`; spend not priced, because Bedrock bills at partner rates |
| Deployment | The same list said "Deployment: none. No cloud URL" while the README gave the URL | "Deployed measurements: none" — the system is deployed, every figure in that document is local, and the older line is acknowledged as having been true when written |
| Held-out set | Four documents called the question set held out | Corrected in phase 2 across the question file, the runner, README, EVALUATION.md, AI_SYSTEM_DESIGN.md and ASSUMPTIONS.md |
| Live accuracy | `37/38 (97.4%)` quoted as current | Withdrawn pending re-measurement, with the reason and the preserved record |
| Demo narration | "Pricing is **refused** for this role" | The pipeline answers with a labelled volume substitute (`status: answered`). Corrected in `scripts/make_demo.py`, so the regenerated transcript says so, and in the recording script |
| Recording guide | Lived inside `docs/DEMO.md`, which `scripts/make_demo.py` **overwrites** | Moved to `docs/DEMO_SCRIPT.md`; regenerating the transcript would have silently deleted it |
| Test counts | README claimed 63, then 148 | 302 total, 115 in the release gate, both checked against a collection count |

One data correction went with it: the working manifest predated the
conversion-factor coverage measurement, so every equivalents answer carried a
"coverage was not measured" caveat. The count was measured against the live
data (0 products affected) and written into the published manifest, rather
than leaving a permanent caveat that says nothing.

---

## Handoff: what is done, and what is next

### Done in this phase — all offline, all local, no AWS or hosted CI

Nine commits from `6c6d632`, each with its own verification:

| Commit | Phase |
|---|---|
| `f8d6d31` | baseline recorded, two headline defects reproduced |
| `0d83a29` | authorization re-evaluated per request; session lifecycle |
| `244d5d0` | evaluation judge repaired; release gate made real |
| `4c005ba` | ratio populations; comparison grains; evidence-based warnings |
| `9312ee5` | intent coverage; typed cohorts; concurrent turns |
| `a4fdb59` | atomic snapshot publication; repeatable reloads |
| `5f09611` | packaging, result-size limit, login throttling, audit contract |
| `64ad68b` | documentation reconciled; handoff |
| `bf371f4` | generic share computed; eval outcomes reported by category; UI evidence from a clean checkout |

Tests went from **148** to **318**, plus 6 jsdom component tests. **58** of the
new ones were run against the unfixed code and observed to fail, which is the
only claim worth making about a regression test:

| Suite | Failed before the fix |
|---|---:|
| `test_session_authorization.py` | 4 of 8 |
| `test_session_lifecycle.py` | 5 of 6 |
| `test_privilege_boundary.py` | 4 of 5 |
| `test_eval_judge.py` | 12 of 18 |
| `test_comparison_grain.py` + `test_coherent_fixture.py` | 4 |
| `test_quality_warnings.py` | 7 of 7 |
| `test_snapshot_publication.py` | 5 of 5 |
| `test_packaging_and_limits.py` | 11 of 12 |
| `identity-isolation.test.jsx` (jsdom) | 6 of 6 |

The rest are regression guards for behaviour that was already correct, or
cover code that did not exist to fail against (`test_declared_grain.py`,
`test_intent_fidelity.py`, `test_cohort_typing.py`, `test_turn_concurrency.py`,
`test_audit_contract.py`, `test_login_throttling.py`, `test_release_gate.py`).
Those are not counted above.

### Getting the source reviewed

The commits are **local**: the brief for this phase said to make local commits
per phase and not to dispatch hosted CI, so nothing was pushed. `origin/main`
is still at `6c6d632`. Two artefacts are produced for review without pushing:

| File | What it is |
|---|---|
| `pharma-analytics-copilot-hardened.zip` | `git archive` of the final tree — 148 files, no `node_modules`, no `.env`, no `evaluator_logins.json`, no generated CSVs |
| `pharma-analytics-copilot-history.bundle` | the full 52-commit history; `git clone <bundle> <dir>` reproduces the repository |

Pushing to `origin` is a separate decision, and the `Co-Authored-By` trailers
should be settled first: removing them afterwards means a force-push over
published history.

### Next, in the order I would do it

1. **Deploy the hardened build.** The running instance is pre-hardening, so the
   deployed system still has the authorization defect fixed in `0d83a29`. This
   is the only item that is a release blocker.
2. **Re-measure live accuracy** under the repaired judge, reported by the five
   categories rather than as one number. Needs paid inference.
3. **Add a proportion metric** ("a filtered subset over the unfiltered whole").
   The oracle and the expected value are in `evals/questions.yaml` under
   `b340-01`. This is the last known capability gap.
4. **Write a genuinely held-out set** — against the supplied documentation,
   sealed before any further tuning, run once.
5. **Add real-browser tests.** The current web tests are jsdom component tests;
   they do not exercise rendering, CSS or actual browser behaviour.
6. **Clear the dev-only npm advisories** via vite 6→8 and vitest 2→5.

### Not attempted, and why

Bedrock or any hosted-model call, deployment, container execution, hosted CI,
and anything touching an AWS account, model access, cost or billing. The brief
for this phase excluded all of it.
