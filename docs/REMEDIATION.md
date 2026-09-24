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
| Contract versions | metric `1.0.0`, policy `1.0.0`, schema contract `1.0.0`, mapping `1.0.0` |

### Defects reproduced against this exact HEAD, before any edit

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
| R04 | Evaluation judge accepts semantic false positives; tuned set described as held out; release command tolerates skips and empty selections | 2 | **fixed** | `tests/unit/test_eval_judge.py` (18 adversarial cases; **12 pass the old judge**) · `tests/unit/test_release_gate.py` (4) | _phase 2_ |
| R05 | `_compile_ratio()` strips the product population from every denominator, so PAP proportion is wrong | 3 | pending | | |
| R06 | Declared grain not enforced (duplicate groups); display limits applied before comparison ranking; label used as identity | 3 | pending | | |
| R07 | Unconditional "competitor-only" market warning; invalid conversion factors yield partial sums presented as totals | 3 | pending | | |
| R08 | Unresolved entities silently broaden the query; percentage/threshold/generic-share intents silently substituted | 4 | pending | | |
| R09 | Follow-up vs fresh question not classified; cohort contents untyped; concurrent turns silently dropped | 4 | pending | | |
| R10 | Rows and manifest publish in separate transactions; vocabulary cache not bound to dataset version; repeated seed loads collide on identity | 5 | pending | | |
| R11 | Failure/audit contract untested over HTTP; result-byte limit unimplemented; packaging omits `metrics.yaml`; UI lacks offline/new-conversation controls | 6 | pending | | |
| R12 | Documentation contradicts itself on deployment, token measurement, run records and benchmark scope; demo narration overstates behaviour | 7 | pending | `docs/DEMO.md` narration says pricing is "refused" for a RAM; the pipeline answers with a labelled volume substitute (status `answered`) | |
| R13 | A 500 from `/api/ask` carried no request id, so a user's report could not be matched to the log line | 1 | **fixed** | `test_api_contract.py::test_a_planner_failure_is_reported_without_internals` | `0d83a29` |
| R14 | The serving process used the **owner** connection on every `ask()` to read the dataset manifest, so the API had to hold owner credentials | 1 | **fixed** | `test_privilege_boundary.py::test_serving_a_request_never_opens_the_owner_connection` | `0d83a29` |
| R16 | Run records stored the plan and SQL but not the answer, so a stored run cannot be re-judged after the judge changes — `amb-01`'s live result could not be rescored | 2 | **fixed** | `scripts/run_evals.py` now records headline, notes, warnings and rows | _phase 2_ |
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
| 8 | `npx vitest run` (`web/`) | jsdom, no network | stubbed fetch | 4 passed (122 ms of test time, 240 s of collection) | A response issued to one identity cannot reach the next one's screen, and signing in clears the transcript and conversation id. |
| 8b | same, against the pre-fix component | jsdom | stubbed fetch | **not obtained** | Four attempts stalled in vitest's collection phase with every process at 0% CPU — the same macOS filesystem stall already diagnosed for a Python venv under `~/Desktop`. Clearing the vite cache, moving it off `~/Desktop` and forcing a single fork did not help; only the first vitest invocation after install ever completed. So R03's fix is evidenced by the passing tests and by the code, not by a measured before/after. Reproducible on a checkout outside `~/Desktop`. |
| 9 | `npm run build` | — | — | built in 15.25 s | The production bundle still builds after the test tooling was added. |
| 10 | `pytest tests/unit/test_eval_judge.py` | offline | fakes only | 18 passed / **12 fail** against the pre-repair judge | The judge now rejects: a boilerplate note standing in for a qualifying one; a packs answer to a percentage question; `warning` with no needle; an empty oracle matching an empty answer; groups the oracle never produced; `no_pricing` judged without ever seeing the SQL; currency in the headline; a typo'd expectation type. |
| 11 | `python3 scripts/run_evals.py` (offline) | offline | `full-182fd9082327` | **36/38**, 2 failed | Under the repaired judge, on the same dataset that previously reported 38/38. The two failures are `b340-01` and `amb-01` — the cases the old rules were hiding. |
| 12 | `pytest tests/security -q --release-gate --min-tests 95` | offline | working + disposable | 95 passed | The gate now fails on a skip or a short collection. Previously `pytest tests/security -q` exited 0 with every test skipped. |
| 13 | `pytest tests -q` | offline | working + disposable | **202 passed** | 148 at baseline, 180 after phase 1, 202 after phase 2. |

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
