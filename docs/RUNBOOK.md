# Runbook

Setup, operation and troubleshooting. Every command here has been run on a clean
checkout on macOS with PostgreSQL 16.

---

## 1. Prerequisites

| Component | Version | Notes |
|---|---|---|
| PostgreSQL | 16+ | 17 works; the migrations use no version-specific syntax |
| Python | 3.11+ | developed on 3.13 |
| Node | 18+ | for the UI build only |
| AWS credentials | — | only for the Bedrock planner; not needed to run offline |

You need a PostgreSQL superuser to create roles and databases once. After that,
the application connects only as non-superuser roles.

---

## 2. First-time setup

```bash
git clone <this repository> && cd pharma-analytics-copilot

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .

# Database, roles, schema, security policies, indexes.
# Writes generated passwords to .env (0600, gitignored).
python3 scripts/bootstrap_db.py --drop

# Full dataset: 40k organizations, 2M sales, ~30k ZIP mappings.
python3 schema/generate_data.py          # ~60s, writes schema/generated/*.csv
python3 scripts/load_data.py --mode full # ~80s

# One evaluator login per role. Passwords are printed ONCE and written to
# evaluator_logins.json (0600, gitignored). Nothing is committed.
python3 scripts/provision_logins.py --demo

npm --prefix web install && npm --prefix web run build
uvicorn app.api.main:app --host 127.0.0.1 --port 8010
```

Open <http://127.0.0.1:8010>.

> **macOS note.** If Python process start-up is inexplicably slow (tens of
> seconds at 0% CPU), the virtualenv is probably under `~/Desktop`,
> `~/Documents` or another TCC-protected folder — macOS revalidates the compiled
> extensions on every process launch. Measured here: 82 s versus 0.11 s for the
> same import. Create the venv outside those folders, e.g. `~/.venvs/pac`.

---

## 3. Configuration

All settings are environment variables prefixed `PAC_`, read from the
environment or `.env`. No secret is ever committed.

| Variable | Default | Purpose |
|---|---|---|
| `PAC_DB_NAME` | `pharma_analytics` | application database |
| `PAC_DB_*_USER` / `PAC_DB_*_PASSWORD` | generated | owner, auth, exec, scoped roles |
| `PAC_LLM_PROVIDER` | `offline` | `bedrock` or `offline` |
| `PAC_BEDROCK_REGION` | `us-east-1` | |
| `PAC_BEDROCK_MODEL_ID` | see below | |
| `PAC_LLM_EFFORT` | `low` | plan extraction is a constrained task |
| `PAC_STATEMENT_TIMEOUT_MS` | `5000` | per-query budget |
| `PAC_MAX_RESULT_ROWS` | `5000` | result cap; the query fetches cap+1 to detect truncation |
| `PAC_SESSION_TTL_HOURS` | `12` | |
| `PAC_COOKIE_SECURE` | `true` | set `false` only for local HTTP |

### Enabling the live planner

```bash
export PAC_LLM_PROVIDER=bedrock
export PAC_BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-5-20251101-v1:0
```

Bedrock requires two things that are **not** code:

1. Valid AWS credentials — check with `aws sts get-caller-identity`.
2. **Anthropic use-case details submitted for the account.** Until this is done
   every invocation fails with
   `Model use case details have not been submitted for this account`.
   Submit it in the Bedrock console under *Model catalog → Submit use case
   details*. It is once per account.

Model IDs must be inference profiles (the `us.` or `global.` prefix) for the
dated releases; bare IDs return
`Invocation ... with on-demand throughput isn't supported`.

---

## 4. Daily operation

```bash
# Health and readiness
curl localhost:8010/health    # {"status":"ok"}
curl localhost:8010/ready     # ready only when a published dataset exists

# Reload data (publishes atomically; a partial load is never queryable)
python3 scripts/load_data.py --mode full

# Switch to the small development fixture
python3 scripts/load_data.py --mode seed

# Rebuild the separate coherent-market test fixture
python3 scripts/build_fixture_db.py

# Tests
python3 -m pytest tests -q               # 148
python3 -m pytest tests/security -q --release-gate --min-tests 95
```

`seed` and `full` are mutually exclusive: each truncates the other's rows,
because the fixture and the generated CSVs reuse the same reference keys.

---

## 5. Verifying the security boundary

Run this after any infrastructure change. It is also checked automatically at
application start-up, and the app refuses to serve if it fails.

```bash
python3 -c "from app.db import verify_runtime_role_safety as v; print(v() or 'boundary intact')"
```

It asserts that no runtime role is `SUPERUSER` or has `BYPASSRLS`, that RLS is
enabled on `organizations` and `sales`, that the scoped role cannot read
`sales.wac` or the `users` table, and that it has no access to the `app_auth` or
`app_conv` schemas.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `no published dataset` | data never loaded, or the load failed | `scripts/load_data.py --mode full`; check `app_meta.dataset_manifest` for a `failed` row |
| `permission denied to create role` | running migrations as `pac_owner`, which deliberately has no `CREATEROLE` | run `scripts/bootstrap_db.py`; roles are created in its admin phase |
| `privilege roles missing` | migration 004 ran before bootstrap | run `scripts/bootstrap_db.py` first |
| A scoped user sees nothing | usually correct — their territory may match no ZIP | check `app_meta.dataset_manifest` warnings for `user_assignment_unmatched`; under seed data 10 of 23 users legitimately resolve to zero rows |
| `canceling statement due to statement timeout` | query exceeded the 5 s budget | narrow the question; a `LIMIT` does not bound the aggregation beneath it |
| Bedrock `AccessDeniedException` | use-case form not submitted, or the model is not enabled for the account | §3 above |
| Bedrock `NotFoundError` from the SDK | wrong Bedrock client for the model generation | dated model IDs use `AnthropicBedrock`; the newest use `AnthropicBedrockMantle` |
| `.env` points at the wrong database | a secondary bootstrap overwrote it | `scripts/bootstrap_db.py --no-env` when provisioning a secondary database |
| Port already in use | another service holds the port | `lsof -nP -iTCP:8010 -sTCP:LISTEN` |

---

## 7. Data refresh

1. Regenerate or drop in new CSVs at `schema/generated/`.
2. `python3 scripts/load_data.py --mode full`.
3. The loader truncates, loads in one transaction, validates, and only then
   marks the snapshot `published` and the previous one `superseded`.
4. Validation distinguishes two outcomes:
   - a **documented anomaly** (impossible share, unmapped ZIP, period mismatch)
     is recorded as a manifest warning and the load proceeds;
   - a condition making safe execution impossible (broken foreign key, unknown
     `data_source`) **fails** the load and the snapshot is never published.
5. `/ready` returns 503 until a published snapshot exists, so a partially loaded
   refresh is never served.

Relative periods are anchored to the manifest's reporting anchor, never the
server clock, so a refresh moves the windows and the same question legitimately
returns a new number.

---

## 8. Backup and restore

```bash
pg_dump -Fc pharma_analytics > pac-$(date +%F).dump
pg_restore -d pharma_analytics --clean --if-exists pac-2026-09-23.dump
```

Roles live outside the database, so re-run `scripts/bootstrap_db.py` (without
`--drop`) after restoring into a fresh cluster, then re-provision logins. The
business data can always be rebuilt from the generator with `SEED = 42`, so the
dump matters mainly for `app_auth`, `app_conv` and `app_meta`.

---

## 9. What to check before calling a deployment good

- `/ready` returns a dataset id.
- `verify_runtime_role_safety()` returns no problems.
- One login per role works, and each sees a different scope.
- A non-Exec revenue question returns volume with the restriction disclosed.
- A market-share question shows the data-quality warning.
- Restart the process: data and logins survive.
- The deployed commit SHA matches what was tested.
