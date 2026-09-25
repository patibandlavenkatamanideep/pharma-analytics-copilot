# Pharma Analytics Copilot

### 🔗 Live: **https://44-217-117-172.sslip.io**

A conversational analytics assistant over a 2,000,000-row pharmaceutical sales
database. Users ask questions in plain English; what they are allowed to see is
enforced by PostgreSQL, not by the application's good intentions.

Built for the NL-to-SQL assessment. The original brief is preserved verbatim at
[`docs/ASSIGNMENT_README.md`](docs/ASSIGNMENT_README.md); all supplied documents,
DDL, seed data and the generator are unmodified.

```
"What are my top 5 accounts by pack units this quarter?"
  → Top 5 by top-level account, ranked on paid pack units.
    Jubilee Clinical Network leads with 1,606 packs.
    Showing data for the New York Metro territory only.
    Reporting window: r3m — the rolling 3 months.
```

---

## Start here

| If you want to | Read |
|---|---|
| Understand the design and the trade-offs | [`DESIGN.md`](DESIGN.md) |
| Understand how the AI layer is designed and bounded | [`docs/AI_SYSTEM_DESIGN.md`](docs/AI_SYSTEM_DESIGN.md) |
| See every decision, with evidence and alternatives | [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) |
| Know where the supplied data contradicts its docs | [`docs/DATA_QUALITY.md`](docs/DATA_QUALITY.md) |
| Run or operate it | [`docs/RUNBOOK.md`](docs/RUNBOOK.md) |
| See test results | [`docs/EVALUATION.md`](docs/EVALUATION.md) |
| See a real transcript, including the refusals | [`docs/DEMO.md`](docs/DEMO.md) — generated, not written |
| Know what was found and fixed in review, and what is still open | [`docs/REMEDIATION.md`](docs/REMEDIATION.md) |

---

## What makes this different from "generate SQL and run it"

**The model never writes SQL.** It picks a metric key, some dimensions and some
filter values from closed vocabularies. The server compiles the SQL from its own
definitions. An unsafe query is not rejected — it is *inexpressible*, because the
plan type has no way to say it.

**Pricing access is a property of the database connection.** Non-Exec roles
connect as a PostgreSQL role that was never granted `SELECT` on `sales.wac`.
Verified: that connection is refused the column through 12 different SQL clauses,
including `ORDER BY` and `SELECT *`. "Sort by revenue but hide the column" fails
at the database, not in a display filter.

**Territory scope is enforced at the facility row, before hierarchy rollup.** A
visible health system cannot pull in its facilities in other territories, because
row-level security filters them before the aggregate ever sees them.

**Bad data is reported, not repaired.** In the supplied dataset every
`market_data` row is a competitor, so the documented market-share denominator is
incomplete and Docetaxel share computes to 113.78%. The system reports that
number with both components and an explicit warning that it is not a real share —
it does not clamp it, and it does not quietly change the formula to make the demo
look better.

---

## Quick start

Requires PostgreSQL 16+, Python 3.11+, Node 18+.

```bash
# 0. Install. `pip install -e .` puts `app` on the path, which every script needs.
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .

# 1. Provision the database, roles and schema.
#    Needs a PostgreSQL superuser once; the application itself never runs as one.
#    Generated passwords are written to .env (0600, gitignored).
python3 scripts/bootstrap_db.py --drop

# 2. Generate the full dataset (40k orgs, 2M sales) and load it
python3 schema/generate_data.py
python3 scripts/load_data.py --mode full        # ~80 seconds

# 3. Create evaluator logins (one per role, passwords printed once)
python3 scripts/provision_logins.py --demo

# 4. Build the UI and serve it with the API from one origin
npm --prefix web install && npm --prefix web run build
uvicorn app.api.main:app --host 127.0.0.1 --port 8010
```

Open <http://127.0.0.1:8010> and sign in with one of the printed accounts.

> **macOS:** if Python start-up is inexplicably slow (tens of seconds at 0% CPU),
> the virtualenv is under a TCC-protected folder such as `~/Desktop` or
> `~/Documents` and macOS is revalidating the compiled extensions on every
> launch. Measured here: 82 s versus 0.11 s. Create it elsewhere, e.g.
> `python3 -m venv ~/.venvs/pac`.

For development against the small fixture instead, use
`scripts/load_data.py --mode seed` — but note that under seed data most RAM
territories match no ZIP at all, so scoped users correctly see nothing. Full data
is the only coherent target for evaluating access control
([why](docs/ASSUMPTIONS.md#a5--seed-data-cannot-exercise-role-scoping-full-data-is-the-real-target)).

---

## Tests

```bash
python3 scripts/build_fixture_db.py          # separate coherent-market fixture
python3 scripts/build_authtest_db.py         # disposable database for the auth tests
python3 -m pytest tests -q                   # 309 tests
python3 -m pytest tests/security -q --release-gate --min-tests 115  # release gate

cd web && npm test                           # 6 jsdom component tests
```

The web tests are **Vitest component tests in jsdom**, not tests in a real
browser: they render the React component and stub `fetch`. They exercise the
identity-isolation logic, not rendering, CSS or actual browser behaviour.

Run them from a checkout **outside `~/Desktop`**. Under `~/Desktop` macOS
stalls the `node_modules` reads: collection takes 239,780 ms and later
invocations hang at 0% CPU, against 59–108 ms from anywhere else. Same
pathology that made a Python venv there take 82 s to `import psycopg`.

Expected values in the integration tests come from SQL written by hand in the
test files, never from the compiler under test.

`--release-gate` is not decoration. Plain `pytest tests/security -q` exits 0
when every test in it *skips*, which is what happens with no database loaded —
a green tick for a run that checked nothing. The flag fails the run if any test
skipped or if fewer than `--min-tests` were collected.

Three databases, kept apart on purpose:

| Database | Contains | Written by tests |
|---|---|---|
| `pharma_analytics` | the working 2M-row dataset | never |
| `pharma_analytics_fixture` | the hand-calculated coherent market | never |
| `pharma_analytics_authtest` | a seed-sized copy, disposable | yes |

The security tests change a user's role, territory and pricing permission to
simulate a permission change, so they run only against the disposable database
and refuse to start if it is pointed at the working one. The identities they
use are created and deleted by the tests; no evaluator credential is read or
rotated.

---

## Layout

```
app/analytics/    metric registry, typed plan, period resolver, compiler, AST validator
app/auth/         identity, sessions, policy
app/data/         ingestion, manifest, derived classification
app/conversation/ owner-scoped structured follow-up state
app/llm/          Bedrock planner + deterministic offline planner
app/pipeline.py   plan → authorize → compile → validate → execute → render → audit
app/api/          HTTP surface
web/              React chat UI, served from the same origin
migrations/       additive PostgreSQL schema, security policies, measured indexes
tests/            unit, integration, coherent fixture, security
schema/           SUPPLIED — untouched
docs/             SUPPLIED business documents — untouched, plus this project's docs
```

---

## Status

Verified on the full dataset: ingestion, the authorization boundary (115 tests),
metric semantics against hand-written reference SQL, and the API and UI served
together.

**Deployed** at <https://44-217-117-172.sslip.io> — one EC2 instance on AWS
with the app, PostgreSQL and Caddy under Docker Compose, real Let's Encrypt
HTTPS, the full 2,000,000-row dataset, and Claude Opus 4.5 on Bedrock.
`infra/smoke.sh` passed against it end to end on 2026-09-24.

That deployment has **not been contacted since**: the hardening work recorded
in [REMEDIATION.md](docs/REMEDIATION.md) was done entirely offline, and none of
the fixes below have been deployed. Current availability is therefore
unverified rather than disproved, and the running instance is the pre-hardening
build.

Live natural-language accuracy was measured at **37/38** on 2026-09-24 against
Claude Opus 4.5 on Bedrock. **That figure is withdrawn pending re-measurement.**
The judge that produced it has since been shown to accept semantic false
positives, and two of the 38 cases passed under rules now known to be vacuous —
one accepted any non-empty result for a question asking for a percentage, the
other accepted a boilerplate note as a qualifying one. The stored run record is
kept as dated historical evidence and the repaired judge is in place, but the
live set has not been re-run (that needs paid inference), so no accuracy number
is claimed here ([EVALUATION.md](docs/EVALUATION.md),
[REMEDIATION.md](docs/REMEDIATION.md)).

### Known open gaps

Recorded rather than rounded off. Full detail in
[REMEDIATION.md](docs/REMEDIATION.md).

| Gap | Effect today |
|---|---|
| No metric expresses "a filtered subset over the unfiltered whole" | "What percentage of volume comes from 340B accounts?" is answered with a count **and a note saying the proportion cannot be computed**, not with 12.27%. Counted as an unsupported request, not as an answer |
| Live accuracy unmeasured under the repaired judge | Needs paid inference; the previous 37/38 is withdrawn, not restated |
| The deployed instance is the **pre-hardening** build | It still has the authorization defect fixed in `0d83a29`. Not redeployed in this phase |
| Dev-only npm advisories (1 critical, 1 high, 3 moderate) | `npm audit --omit=dev` is clean, so nothing ships in `dist/`; clearing them needs a vite 6→8 toolchain migration |
| Single host, no redundancy; deployed latency under concurrency unmeasured | See [DESIGN.md §12](DESIGN.md#12-status-and-what-is-not-yet-proven) |

This is not called production-ready while those remain.
