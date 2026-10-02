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
| `PAC_BUSINESS_TIMEZONE` | `America/New_York` | the day an ingested sale belongs to ([INGESTION.md](INGESTION.md)) |
| `PAC_INGEST_MAX_QUARANTINE_RATIO` | `0.05` | above this share of invalid events a batch is rejected |
| `PAC_OTEL_ENDPOINT` | unset | OTLP/HTTP collector; unset exports nothing ([OBSERVABILITY.md](OBSERVABILITY.md)) |
| `PAC_RELEASE` | `dev` | release identifier reported with telemetry |
| `PAC_LOG_FORMAT` / `PAC_LOG_LEVEL` | `json` / `INFO` | one sanitised JSON line per record ([OBSERVABILITY.md](OBSERVABILITY.md#logs)); `text` only for local work |
| `PAC_ADMISSION_MAX_INFLIGHT_REQUESTS` | `24` | questions in flight per worker; beyond it, 503 `overloaded` at once |
| `PAC_ADMISSION_QUERY_SLOTS` / `_QUEUE` / `_WAIT_SECONDS` | `4` / `16` / `10` | analytical queries running / waiting per worker, and the longest wait ([CAPACITY.md](CAPACITY.md#admission-control)) |
| `PAC_CONVERSATION_RETENTION_DAYS`, `PAC_AUDIT_RETENTION_DAYS`, … | 180, 400, … | retention periods, applied by `scripts/prune_state.py` ([RETENTION.md](RETENTION.md)) |

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

### Enabling single sign-on

Off by default. Password sign-in is unaffected either way.

| Variable | Purpose |
|---|---|
| `PAC_OIDC_ENABLED` | `true` to offer SSO |
| `PAC_OIDC_ISSUER` | the issuer exactly as the provider publishes it |
| `PAC_OIDC_CLIENT_ID` / `PAC_OIDC_CLIENT_SECRET` | the registered client; a secret only for a confidential client |
| `PAC_OIDC_REDIRECT_URI` | `https://<host>/api/auth/oidc/callback`, registered with the provider |
| `PAC_OIDC_ALGORITHMS` | `RS256,ES256`; never `none` or HMAC |
| `PAC_OIDC_LINK_BY_VERIFIED_EMAIL` | `false`; see `app/auth/oidc.py` before enabling |

Accounts are linked by `(issuer, subject)` in `app_auth.identities`. An
administrator creates the link.

**Browser binding.** `GET /api/auth/oidc/start` sets an HttpOnly cookie,
`__Host-pac_oidc` (Secure, `Path=/`, `SameSite=Lax`, 10 minutes). The
callback is refused with `browser_mismatch` unless it arrives with that
cookie, and the check happens before the code is exchanged. This is what
stops a callback obtained in one browser from signing in another (login
CSRF). Consequences for operators:

- The application must be served over HTTPS with `PAC_COOKIE_SECURE=true`.
  Without Secure the browser rejects a `__Host-` cookie.
- The provider must return to the callback with a top-level `GET`, the
  default `response_mode=query`. `form_post` is a cross-site POST, which
  does not carry a `SameSite=Lax` cookie, so it is not supported.
- Sign-ins started in two tabs of one browser can both finish. Starting in
  one browser and finishing in another cannot.

**Verifying with a real provider (staging).** The tests use an in-process
provider (`tests/security/fake_idp.py`) that checks PKCE, nonce, signatures
and single-use codes. They do not establish interoperability with a
specific provider or its MFA policy. With a registered client on staging,
check and record each of these:

1. A linked user completes sign-in and `GET /api/me` shows the right role
   and scope.
2. The start response sets `__Host-pac_oidc` with `Secure; HttpOnly;
   SameSite=Lax; Path=/` (browser developer tools).
3. Copy the callback URL from browser A, before it loads, into a private
   window B. B receives `400 browser_mismatch` and is not signed in. A can
   still finish.
4. Cancel at the provider. The callback answers `400 provider_declined`,
   and going back to the same attempt answers `invalid_state`.
5. Wait more than 10 minutes on the provider's page, then finish. The
   callback answers `invalid_state`.
6. Disable the account, then sign in again. The callback answers
   `403 disabled`.
7. The provider's signing-key rotation (where it can be triggered) is
   picked up without a restart.

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
python3 -m pytest tests/security -q --release-gate --min-tests 394
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
| `/ready` says `database unavailable`, API returns `503 database_unavailable` | PostgreSQL unreachable, credentials wrong, or the pool exhausted | the server log names the error class; `pac.db.pool.timeouts` and `pac.db.errors{kind="unavailable"}` separate exhaustion from outage ([OBSERVABILITY.md](OBSERVABILITY.md)) |
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

### Incremental batches

New, corrected and deleted sales can be applied without a full reload, run
from the jobs container:

```bash
python3 scripts/ingest.py batch-0001.json      # exit 1 if a batch was rejected
```

Each batch is reconciled against its declared totals, validated (invalid
events quarantined with a reason), applied by event identity and version,
and published as a new generation in one transaction. A replay changes
nothing. A batch that adds a new week rewrites every fact's week offset;
on the full dataset that took 124 s idle and about 7 minutes under load,
and readers kept answering throughout ([CAPACITY.md](CAPACITY.md)). The contract, the outcomes table, recovery and the
measurements are in [INGESTION.md](INGESTION.md).

A full or seed load clears the ingestion ledger, so retained batches can be
replayed onto the new base.

Schedule a freshness check next to the feed, independent of it. For a
daily feed:

```bash
python3 scripts/ingest.py --check-freshness --max-since-success 26h --source <feed>
```

Exit 3 means a missed run (`missed_run`), data that stopped moving
(`stale_data`) or a feed that never delivered (`never_delivered`). The job
also exports its batches and freshness when `PAC_OTEL_ENDPOINT` is set in
the jobs container ([OBSERVABILITY.md](OBSERVABILITY.md#freshness)).

---

## 8. Backup and restore

```bash
# Backup: one consistent snapshot; readers and writers are not blocked.
pg_dump -Fc -f pac-$(date +%F).dump pharma_analytics

# Restore into a NEW database, check it, then point the app at it.
createdb -O pac_owner pharma_analytics_restored
pg_restore -d pharma_analytics_restored -j 4 pac-2026-10-01.dump
PAC_DB_NAME=pharma_analytics_restored python3 -c \
  "from app.db import verify_runtime_role_safety as v; print(v() or 'boundary intact')"
```

**Drill.** `scripts/restore_drill.py` does all of this against a disposable
target and checks the result. Row counts, the published generation,
row-level-security policies and grants must match the source. The runtime
boundary check must pass, the restored database must report ready, and the
same questions, asked as a RAM and as an Exec, must get the same answers.
Measured on 2026-10-01 from a full-size copy (2,002,000 sales) on the
development machine (`evidence/runs/r2-restore-drill.json`):

| Step | Time |
|---|---:|
| `pg_dump -Fc` (45.5 MB) | 4.5 s |
| `pg_restore -j 4` | 18.5 s |
| Restore to ready, every check passed | **22.4 s** |

This proves the **procedure**, not a recovery objective. The 22.4 s is a
restore within one cluster on a developer machine, from a dump already on
local disk. It does not demonstrate:

- point-in-time recovery;
- recovery after losing the host or its disk;
- fetching a backup from off-host storage;
- re-provisioning roles in a new cluster;
- repointing a deployment.

A production RTO is the sum of those steps, measured in the target
environment.
**RPO** is the interval between backups: a nightly `pg_dump` loses up to a
day. Anything tighter needs WAL archiving with point-in-time recovery, or a
managed database that provides it. Neither is configured here, and no RPO
has been agreed.

Restoring into a **new cluster** needs the roles first: run
`scripts/bootstrap_db.py` (without `--drop`) before `pg_restore`, then
re-provision logins. The drill restored within the same cluster, where
the roles already existed, so that path is not measured.

The dump matters most for `app_auth`, `app_conv`, `app_meta` and
`app_ingest`. The business data can be regenerated (`SEED = 42`), but
incremental batches are not stored server-side. A source must keep its
batches so they can be replayed after a restore ([INGESTION.md](INGESTION.md)).
Restored backups still hold data users have since deleted until the backups
themselves expire ([RETENTION.md](RETENTION.md)).

---

## 9. Connections, replicas and shutdown

**Connection budget.** Each worker process opens these pools:

| Pool | Role | Max |
|---|---|---:|
| exec | `pac_exec_login` | 8 |
| scoped | `pac_scoped_login` | 8 |
| auth | `pac_auth_login` | 4 |
| graph (checkpoints) | `pac_auth_login` | 4 |
| **per worker** | | **24** |

The image runs 2 workers, so one replica uses up to **48**. The jobs
container (owner pool, max 4) runs alongside on demand. PostgreSQL's default
`max_connections` is 100, which is therefore **one replica plus jobs**.
Before adding replicas, either raise `max_connections` or put PgBouncer in
transaction mode in front. Budget at least
`replicas × workers × 24 + 4 + superuser_reserved_connections`. The scoped
and exec pools are where requests queue under load (see
[CAPACITY.md](CAPACITY.md)). Their size is the backpressure point. In the
load profile, all 16 scoped connections were active from 16 clients
upward, and that is where expensive questions began to hit the statement
timeout. Raising the pool size moves the queue into the database rather
than removing it; measure before changing it.

**What is safe across replicas.** Every piece of shared state lives in
PostgreSQL:

- **Quotas:** counted in `app_conv.runs`, under an advisory lock.
- **One live request per conversation:** a run lease.
- **One outcome per idempotency key:** a unique index.
- **Sessions, clarifications and checkpoints.**
- **Ingestion and loads:** one advisory publication lock.

The in-process caches (vocabularies, entity indexes) are keyed by dataset
id and scope, so a replica never serves another generation's names. Not
verified: running more than one replica, which no environment here has
done.

**Shutdown.** On SIGTERM uvicorn lets in-flight requests finish for up to
65 s, longer than the 60 s request deadline. Compose's `stop_grace_period`
is 75 s. The app then flushes telemetry within its export timeout and
closes its pools.

`scripts/drain_check.py` measured this. With four slow answers in flight at
the signal, all four were answered and the process exited 4 s later
(`evidence/runs/r2-drain.json`).

It also stops accepting new connections, but not at the instant of the
signal. A request made 0.3 s after it was served in some runs and not in
others. So take a replica out of the load balancer (or let readiness fail)
before stopping it, rather than relying on the signal to turn traffic away.

A request still running after the 65 s window loses its lease. Its
idempotency key can be retried, and the retry resumes from the last
checkpoint.

---

## 10. What to check before calling a deployment good

- `/ready` returns a dataset id.
- `verify_runtime_role_safety()` returns no problems.
- One login per role works, and each sees a different scope.
- A non-Exec revenue question returns volume with the restriction disclosed.
- A market-share question shows the data-quality warning.
- Restart the process: data and logins survive.
- The deployed commit SHA matches what was tested.
