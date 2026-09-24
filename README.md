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
python3 -m pytest tests -q                   # 148 tests
python3 -m pytest tests/security -q          # the release gate
```

Expected values in the integration tests come from SQL written by hand in the
test files, never from the compiler under test.

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

Verified on the full dataset: ingestion, the authorization boundary (63 tests),
metric semantics against hand-written reference SQL, and the API and UI served
together.

**Deployed and running** at <https://44-217-117-172.sslip.io> — one EC2
instance on AWS with the app, PostgreSQL and Caddy under Docker Compose, real
Let's Encrypt HTTPS, the full 2,000,000-row dataset, and Claude Opus 4.5 on
Bedrock. `infra/smoke.sh` passes against it end to end.

Live natural-language accuracy is **37/38 (97.4%)** on a held-out question set —
measured, with the one miss recorded rather than rewritten away
([EVALUATION.md](docs/EVALUATION.md)).

Honest limitations: a single host has no redundancy, and deployed latency under
concurrency has not been measured. See
[DESIGN.md §12](DESIGN.md#12-status-and-what-is-not-yet-proven).
