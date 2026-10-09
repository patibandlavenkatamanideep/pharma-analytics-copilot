# Test inventory

Every other document points here for counts instead of stating its own.
That is the whole reason this file exists: at the time it was written,
`README.md` said 368 tests, `DESIGN.md` said 148, `docs/REQUIREMENTS.md`
said 377 and `docs/EVALUATION.md` said 148. The real number was 503. Four
numbers drifted because each was typed by hand in a different week, and a
reader had no way to tell which one was current.

`tests/unit/test_documented_counts.py` fails if the counts below stop
matching what pytest collects, and fails if another document reintroduces a
total of its own.

**Counts** are what pytest collects at the current commit of
`codex/release-defects-oct06`; the runs that establish each result, with
their commit and tree state, are listed in
[RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md).

---

## What each layer establishes

These are five different kinds of evidence, and collapsing them into one
number hides which kind is missing. A green unit suite says nothing about
the authorization boundary; a green security suite says nothing about
whether an answer is arithmetically right.

| Layer | Command | Tests | Result | Evidence |
|---|---|---:|---|---|
| **Unit** | `pytest tests/unit -q` | 860 | pass, no skips | `r5-rc-a786bda-pytest.json` (`a786bda`); without a database too, `r5-rc-a786bda-unit-nodb.json` |
| **Integration** | `pytest tests/integration -q` | 970 | pass, no skips | `r5-rc-a786bda-pytest.json` (`a786bda`) |
| **Security** | `pytest tests/security -q --release-gate --min-tests 450` | 450 | pass, strict gate | `r5-rc-a786bda-security.json` (`a786bda`) |
| **Total (pytest)** | `pytest tests -q --release-gate` | **2280** | pass, strict gate: no skips, no xfails | `r5-rc-a786bda-pytest.json` (`a786bda`) |
| **Browser — component** | `cd web && npm test` | 32 | pass, strict gate | `r5-rc-a786bda-component.json` (`a786bda`) |
| **Browser — end to end** | `python3 scripts/browser_journeys.py` | 15 | pass, no skips | `r5-rc-a786bda-browser.json` (`a786bda`) |
| **Model evaluation** | see the table below | 62 checks | 62 pass (offline planner) | `r5-rc-a786bda-eval-*.json` (`a786bda`, offline); `7950e71` had 61 pass / 1 fail (k-07, fixed in step 2) |

Unit and integration counts are what pytest collects, not what anyone
remembers.

15 of the unit tests (`tests/unit/test_staging_infra.py`) read the AWS
staging module's Terraform source and its cost estimate: what a change would
have to remove to open the database, run a privileged container, put a
password in plain environment or let CI deploy. They need neither AWS nor a
database, and they do not establish that the module applies.

**Read the integration count with care.** 672 of its cases are the
compatibility matrix (`tests/integration/test_compatibility_matrix.py`):
every metric against every grain, every filter family and a comparison
window, each required to be refused by name or to compile, pass the SQL
validator and be **planned** by PostgreSQL (`EXPLAIN`). They prove no
combination reaches the database as an error; they do not check a single
result. The other 233 do check results or behaviour:

- most compare answers with SQL written by hand;
- 40 apply real batches to a disposable database
  (`tests/integration/test_ingestion.py`);
- 6 trace real requests (`tests/integration/test_telemetry_pipeline.py`);
- 57 feed malformed and non-finite JSON through the real batch adapter
  into PostgreSQL, including envelope rejection that leaves published data
  intact, replay, and corrections beside bad records (`tests/integration/test_ingest_contract.py`);
- 11 run the real ingestion command against a local OTLP receiver, a
  hanging one and none, and check freshness after a feed stops
  (`tests/integration/test_ingest_observability.py`);
- 24 inject provider and database failures, or an exhausted spend, or test the
  website's shared model allowance
  (`tests/integration/test_failure_modes.py`);
- 5 exercise admission control on the real request path
  (`tests/integration/test_admission_pipeline.py`).

The security count is also the `--min-tests` floor the release
gate enforces, so the two cannot drift apart without the gate failing.

### What each layer does **not** establish

| Layer | Does not establish |
|---|---|
| Unit | Anything about real data volumes, the authorization boundary, or the live model |
| Integration | Live model planning; it uses the deterministic offline planner |
| Security | That an answer is *correct* — only that it is *permitted* |
| Browser | Anything about analytical correctness |
| Model evaluation (offline) | Live model behaviour. It measures the compiler, the metric registry and the judge |

---

## Model evaluation, by suite

Separated because they answer different questions and have very different
standing. The live column is history: it was measured on an earlier build
and prompt, and prompt 2.3.0 on this branch has not been run live
([EVALUATION.md](EVALUATION.md)). Only `holdout2` was ever unseen at the moment it was first run,
so it is the only figure that is not, to some degree, a measure of work
done against the questions.

| Suite | Checks | Offline (candidate `a786bda`) | Live, **historical** (2026-09-25, commit `7e91f9f`, unversioned prompt before 2.1.0) | Standing |
|---|---:|---|---|---|
| `evals/questions.yaml` — regression set | 38 | **38/38** | 37/38 | Developed against. A regression guard, not an accuracy estimate |
| `evals/holdout.yaml` — held-out set 1 | 12 | **12/12** | 11/12 | Sealed, then run. Fixes were made afterwards, so it is no longer unseen |
| `evals/holdout2.yaml` — held-out set 2 | 12 | **12/12** (11/12 on `7950e71`) | 10/12 | First run scored **8/12**. That is the only unbiased figure in this table |

The offline failure on `7950e71`, `holdout2` / **k-07** — *"Is Zenovax
volume growing or declining month over month?"* — was recorded as a failure
rather than explained away as a limit of the keyword planner. Step 2 of the
qualification found the cause (a monthly period-over-period request could not
be planned), fixed it generally (`fce6b93`) with independent numerical tests,
and kept the old outcome and oracle on record
([QUALIFICATION_2026_10_07.md](QUALIFICATION_2026_10_07.md)). `holdout2` is
spent: 12/12 now measures regressions, not unseen accuracy.

A check that passes by correctly declining or clarifying is **right
behaviour, not accuracy**, and the runner reports those separately.

Live figures are from 2026-09-25 against Claude Opus 4.5 on Bedrock and are
**not** re-measured on this revision — paid inference is out of scope for
this work. They are labelled with the date they were taken.

---

## Browser tests

**Component tests (32, vitest + jsdom).** The 22 conversation/identity cases cover: six for identity isolation, eight for the API v2 contract (idempotent retry, Stop, clarification choices, the not-saved notice, an ended session, busy and rate-limited responses), two for offering single sign-on only when configured, one for re-asking once when the data was refreshed mid-answer, one for showing the date the data runs through, two for deleting a conversation and downloading one's own data, one for sending feedback on an answer, one for waiting out an overload before retrying.

Six chart cases cover returned-row scope and labels, negative values, missing/time-ordered values, truncation disclosure, unsupported shapes, and no bar for a share stated as a range. Four table cases cover each period against the one before it (prior, change, percentage), the provisional period, a plain series without change columns, and a segment share with volume of unknown class (that volume and the range). The final run uses Node 24.19.0 and the locked install.

The following cache diagnosis is historical. They had not run from this checkout for weeks, and the recorded reason was
wrong. The symptom: the vitest worker starts, never responds, and the run
ends after 60 s having collected nothing — under both the `forks` and the
`threads` pool. The recorded explanation was that macOS stalls reads under
`~/Desktop`, which every observation appeared to support, because every
fresh checkout used to test the theory worked.

A fresh checkout also has a fresh cache. Isolating the variables:

| Where | Result |
|---|---|
| A plain Node worker under the repository path | replies in **11 ms** |
| The tree copied to `/private/tmp` | **6 passed, 503 ms** |
| The tree copied to `~/Desktop`, outside this repository, `node_modules` symlinked back into it | **6 passed, 578 ms** |
| The repository itself, after `rm -rf web/node_modules/.vite` | **6 passed, 509 ms**, then 456 / 376 / 375 ms |

That historical comparison identified a cache problem: a stale entry in
`web/node_modules/.vite` hung the worker. `npm test` now clears that
directory before running. The suite takes under half a second, which does
not need a cache.

**End-to-end tests (10, Playwright + Chromium).** These pass here, against
a real server and the disposable database, in about six seconds:
`scripts/browser_journeys.py` provisions disposable identities -- and two
clinics sharing a name, for the clarification journey -- starts the
application, runs Playwright, and removes everything it created. Journeys:
a priced answer rendered, reload keeps the session, sign-out survives
reload, follow-up and New conversation, a RAM sees no currency, an
out-of-scope territory refused, identity change leaves nothing on screen,
a conversation belongs to its owner, a clarification answered by choosing,
an ended session returns to sign-in. CI runs them in the test job.

Neither browser suite is counted in the pytest total: they do not run in the same
command, and a single number that mixed them would imply they do.

---

## CI, and the skips it used to allow

`.github/workflows/ci.yml` is the reference invocation. Two things about
its history are worth stating plainly, because an earlier version of this
documentation implied a stronger guarantee than CI actually gave.

1. **The security job did not run the gate.** It was
   `python -m pytest tests/security -q` — no `--release-gate`, no
   `--min-tests`. With no database present every test in that job **skips**,
   and a run in which every test skipped exited **0**. The job had been
   green for its entire history without that meaning the authorization
   boundary held. Recorded as **D3**, fixed in Phase 1B.

2. **The disposable database the security tests need was never built.**
   `scripts/build_authtest_db.py` was not in the workflow, so the tests that
   depend on it could only ever have skipped. Adding the gate without adding
   the build step would have turned a silent pass into a hard failure —
   which is the correct outcome, and the reason both changes landed
   together.

The gate itself had a third hole, independent of CI: it converted a skip to
a failure only when the skip was reported during **setup**. A
`pytest.skip()` inside a test body skips at **call** phase, and a
module-level skip produces no items at all. Both exited 0 under
`--release-gate`. Recorded as **D1**, fixed in Phase 1B, and held closed by
15 subprocess tests in `tests/unit/test_release_gate_strict.py`.

What is true now: the security job builds its database, runs under
`--release-gate --min-tests 450`, and fails on a skip in any phase, a
module-level skip, a collection error, an unapproved xfail or xpass, a
narrowed selection, or a session in which nothing passed.

CI now includes both browser suites. Hosted CI has not run for this local branch, and live-model evaluation remains unverified.

---

## Reproducing this table

```bash
python3 scripts/build_fixture_db.py     # coherent-market fixture
python3 scripts/build_authtest_db.py    # disposable authorization database

python3 -m pytest tests/unit -q
python3 -m pytest tests/integration -q
python3 -m pytest tests/security -q --release-gate --min-tests 450
python3 -m pytest tests -q              # the total

python3 scripts/run_evals.py --provider offline
python3 scripts/run_evals.py --provider offline --questions evals/holdout.yaml
python3 scripts/run_evals.py --provider offline --questions evals/holdout2.yaml
```
