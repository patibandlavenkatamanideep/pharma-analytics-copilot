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

**Measured on** `7292f42` (`post-assessment/production-readiness`),
offline provider mode, dataset fingerprint `cba52562c89a4f8fd76c52a3`.

---

## What each layer establishes

These are five different kinds of evidence, and collapsing them into one
number hides which kind is missing. A green unit suite says nothing about
the authorization boundary; a green security suite says nothing about
whether an answer is arithmetically right.

| Layer | Command | Tests | Result | Evidence |
|---|---|---:|---|---|
| **Unit** | `pytest tests/unit -q` | 652 | ✅ pass | `r2-image-pytest.json` |
| **Integration** | `pytest tests/integration -q` | 815 | ✅ pass | `r2-suite-7c-integration.json` |
| **Security** | `pytest tests/security -q --release-gate --min-tests 364` | 364 | ✅ pass | `r2-final2-security.json` |
| **Total (pytest)** | `pytest tests -q` | **1831** | ✅ pass | `r2-image-pytest.json` |
| **Browser — component** | `cd web && npm test` | 21 | ✅ pass | `r2-final2-component.json` |
| **Browser — end to end** | `python3 scripts/browser_journeys.py` | 10 | ✅ pass | `r2-final2-browser.json` |
| **Model evaluation** | see the table below | 62 checks | 61 pass / 1 fail | `p1d-eval-*.json` |

Unit and integration counts are what pytest collects, not what anyone
remembers.

**Read the integration count with care.** 672 of its cases are the
compatibility matrix (`tests/integration/test_compatibility_matrix.py`):
every metric against every grain, every filter family and a comparison
window, each required to be refused by name or to compile, pass the SQL
validator and be **planned** by PostgreSQL (`EXPLAIN`). They prove no
combination reaches the database as an error; they do not check a single
result. The other 143 do check results or behaviour:

- most compare answers with SQL written by hand;
- 40 apply real batches to a disposable database
  (`tests/integration/test_ingestion.py`);
- 6 trace real requests (`tests/integration/test_telemetry_pipeline.py`);
- 10 inject provider and database failures
  (`tests/integration/test_failure_modes.py`).

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
standing. Only `holdout2` was ever unseen at the moment it was first run,
so it is the only figure that is not, to some degree, a measure of work
done against the questions.

| Suite | Checks | Offline | Live (2026-09-25) | Standing |
|---|---:|---|---|---|
| `evals/questions.yaml` — regression set | 38 | **38/38** | 37/38 | Developed against. A regression guard, not an accuracy estimate |
| `evals/holdout.yaml` — held-out set 1 | 12 | **12/12** | 11/12 | Sealed, then run. Fixes were made afterwards, so it is no longer unseen |
| `evals/holdout2.yaml` — held-out set 2 | 12 | **11/12** | 10/12 | First run scored **8/12**. That is the only unbiased figure in this table |

Offline failure, `holdout2` / **k-07** — *"Is Zenovax volume growing or
declining month over month?"*: the offline planner chooses
`volume_growth` where the oracle expects `paid_pack_units`. The offline
planner is a keyword matcher, so this is a limit of the stand-in rather than
of the system; the live model answers it. Recorded as a failure regardless,
because a suite that is allowed to explain away its own failures measures
nothing.

A check that passes by correctly declining or clarifying is **right
behaviour, not accuracy**, and the runner reports those separately.

Live figures are from 2026-09-25 against Claude Opus 4.5 on Bedrock and are
**not** re-measured on this revision — paid inference is out of scope for
this work. They are labelled with the date they were taken.

---

## Browser tests

**Component tests (21, vitest + jsdom).** These pass here, in about a second: six for identity isolation, eight for the API v2 contract (idempotent retry, Stop, clarification choices, the not-saved notice, an ended session, busy and rate-limited responses), two for offering single sign-on only when configured, one for re-asking once when the data was refreshed mid-answer, one for showing the date the data runs through, two for deleting a conversation and downloading one's own data, one for sending feedback on an answer.

They had not run from this checkout for weeks, and the recorded reason was
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

So it was never the path and never the filesystem: a stale entry in
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

Neither browser suite is counted in the 1831: they do not run in the same
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
`--release-gate --min-tests 364`, and fails on a skip in any phase, a
module-level skip, a collection error, an unapproved xfail or xpass, a
narrowed selection, or a session in which nothing passed.

What is still **not** true: CI does not run the browser suites, and does not
run live-model evaluation.

---

## Reproducing this table

```bash
python3 scripts/build_fixture_db.py     # coherent-market fixture
python3 scripts/build_authtest_db.py    # disposable authorization database

python3 -m pytest tests/unit -q
python3 -m pytest tests/integration -q
python3 -m pytest tests/security -q --release-gate --min-tests 364
python3 -m pytest tests -q              # the total

python3 scripts/run_evals.py --provider offline
python3 scripts/run_evals.py --provider offline --questions evals/holdout.yaml
python3 scripts/run_evals.py --provider offline --questions evals/holdout2.yaml
```
