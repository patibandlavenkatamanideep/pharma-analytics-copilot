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
| R01 | History/list/titles authorized only by owner, not by current scope or pricing permission; stored headlines can carry WAC amounts and old-territory labels | 1 | pending | | |
| R02 | `resolve()` ignores disabled credentials; cookie name inconsistently applied; rotation/revocation undefined; startup privilege checks test fixed role names rather than effective grants | 1 | pending | | |
| R03 | A delayed response from a previous identity can update the UI after an account switch | 1 | pending | | |
| R04 | Evaluation judge accepts semantic false positives; tuned set described as held out; release command tolerates skips and empty selections | 2 | pending | | |
| R05 | `_compile_ratio()` strips the product population from every denominator, so PAP proportion is wrong | 3 | pending | | |
| R06 | Declared grain not enforced (duplicate groups); display limits applied before comparison ranking; label used as identity | 3 | pending | | |
| R07 | Unconditional "competitor-only" market warning; invalid conversion factors yield partial sums presented as totals | 3 | pending | | |
| R08 | Unresolved entities silently broaden the query; percentage/threshold/generic-share intents silently substituted | 4 | pending | | |
| R09 | Follow-up vs fresh question not classified; cohort contents untyped; concurrent turns silently dropped | 4 | pending | | |
| R10 | Rows and manifest publish in separate transactions; vocabulary cache not bound to dataset version; repeated seed loads collide on identity | 5 | pending | | |
| R11 | Failure/audit contract untested over HTTP; result-byte limit unimplemented; packaging omits `metrics.yaml`; UI lacks offline/new-conversation controls | 6 | pending | | |
| R12 | Documentation contradicts itself on deployment, token measurement, run records and benchmark scope; demo narration overstates behaviour | 7 | pending | | |

---

## Verification ledger

Every row records the command, the environment, the fixture, and **what the
result proves** — which is deliberately narrower than "it passed".

| # | Command | Env | Dataset / fixture | Result | Proves |
|---|---|---|---|---|---|
| 0 | `pytest tests -q` | offline, no AWS creds | `full-182fd9082327` | 148 passed | Baseline before edits. Does **not** prove authorization under permission change, nor evaluator soundness. |
| 1 | hand SQL vs compiler, PAP | offline | coherent fixture | 20/175 ≠ 20/130 | R05 reproduced. |
| 2 | pipeline, b340-01 / amb-01 | offline | `full-182fd9082327` | packs / company-brand share | R04 reproduced. |

---

## Scope explicitly deferred to a later phase

Recorded so nothing is silently dropped:

- Bedrock/provider adapter integration testing (no hosted model calls here).
- Deployed acceptance tests and external availability verification.
- Container/Compose execution and persistence checks.
- Hosted CI runs.
- Any account, model-access, cost or billing review.

The existing deployment record and `evals/runs/reference-bedrock-full.json`
(37/38) are **preserved as dated historical evidence**. The reviewer did not
contact the deployment, and neither does this phase; current availability is
therefore unverified rather than disproved.
