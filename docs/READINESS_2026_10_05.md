# Local NL2SQL readiness review — 5 October 2026

## Baseline and scope

Origin was verified as `patibandlavenkatamanideep/pharma-analytics-copilot`.
Remote refs still exposed only main at `c8aab5b`. The clean Mac checkout was
`de072e0`; the supplied local bundle was verified and imported. Work uses
`codex/nl2sql-readiness` from `58b3d3ec7c87012867ee02e409aad80e5a4c6d9a`,
including all R1–R5 fixes and final code `45db088`. Existing branches, `.env`
and the working database were preserved. No push, deployment or paid model
call was authorized or performed. No AVYXA code was used.

The first local bundle import stalled on iCloud-backed Git objects. The same
remote base and bundle were assembled in a temporary Git object store and
imported without rewriting any history. Python 3.13.2 dependencies were
installed into `/private/tmp/pac-readiness-venv` from `requirements.lock`
with `--require-hashes`. PostgreSQL 16 runs in a separate disposable cluster
on localhost port 55438; the full generated dataset and separate authorization,
ingestion and coherent-market fixtures are used. Test-run evidence records
its actual environment, dependency hash, prompt fingerprint and commit.

## Confirmed checkpoint findings

`langgraph-checkpoint==4.2.0`, resolved by the hash lock, defaults to
warn-and-allow MessagePack constructors unless the import-time environment
requests strict loading. `PostgresSaver` inherited that default. The app's
actual runtime saver invoked a harmless `builtins.print` marker embedded in
checkpoint bytes. A fresh Python process with permissive environment did so
as well. This requires checkpoint write access; it is not evidence that a
normal chat user can write checkpoints or execute code.

`evidence/runs/r4-checkpoint-reproduced.json` records five failed regression
checks against unchanged application code at `58b3d3e`. Its dirty-tree hashes
identify the added probe tests. A malformed PostgreSQL checkpoint returned a
500 during `_entry`, outside the run cleanup handler; an invalid primitive
shape also reached the resumed node. The probe uses no shell execution,
network call or real secret.

The runtime saver now explicitly configures the supported restrictive
`JsonPlusSerializer` API. A preflight pass accepts only primitive data and
LangGraph's required `Interrupt` envelope: strict library mode alone silently
substitutes blocked constructor arguments. Unknown extensions, pickle, JSON
constructors, oversized payloads and invalid state shapes are refused. Saved
turn fields are validated before resumption and each node, including the
interrupt node. Loading is inside the leased operation's error handler. An
unreadable saved turn yields a safe error, no answer, and a failed run with no
busy lease. A new question can proceed. Corrupt paused state stays subject to
existing retention; it is not silently executed or replanned.

This is deserialization hardening and structural validation, not checkpoint
integrity authentication. An actor who can rewrite valid database state can
still alter primitive values. PostgreSQL role isolation, current-principal
checks, compiler validation and RLS remain required. Source schemas, metric
formulas, the typed planner contract and audit availability policy are unchanged.

## Capability inventory

| Capability | Implementation / regression coverage | Remaining boundary |
|---|---|---|
| Orchestration and clarification | `app/graph/turn.py`, `app/pipeline.py`; `test_turn_graph.py`, `test_graph_durability.py`, `test_checkpoint_loading.py` | Real replica termination/recovery in staging |
| Conversation ownership, scope, version, cohort and concurrency | `app/conversation/`, `app/analytics/continuity.py`; `test_conversation_reliability.py`, graph and security suites | Replica routing, restarts and refresh under deployment load |
| Identity and database authorization | `app/auth/`, OIDC migration 020, policy/compiler and RLS; `tests/security/` including R1 regressions | Real IdP registration, MFA and key rotation |
| Per-user quotas and operational bounds | `app/conversation/runs.py`, migration 021, `app/admission.py`, planner deadlines; `test_retry_quotas.py`, `test_run_limits.py`, admission suites | Shared database quotas are deployment-wide; admission slots are per process. Size workers × replicas against real model latency |
| Ingestion replay/correction/quarantine | `app/data/sources.py`, `app/data/ingest.py`, migration 022; ingestion contract/calendar/observability suites | Real retained source batches and scheduler. Feed event identity is separate from entity resolution and HTTP idempotency |
| Typed plans and analytics semantics | `app/analytics/`, `app/llm/planner.py`; unit/integration/security suites and offline evaluation | Offline planner is not evidence of language accuracy |
| Evaluation | `scripts/run_evals.py`, freeze tooling and `app/llm/token_bound.py`; budget tests | New frozen unseen holdout, secure credentials, rates and explicit spend cap for prompt 2.1.0 |
| Observability and feedback | `app/telemetry.py`, `app/logging.py`, `/api/feedback`; telemetry/log/ingestion and audit tests | Hosted OTLP receiver, metrics backend, dashboard and incident drill |
| Release operations | Actual Dockerfile/compose configuration, `.github/workflows/ci.yml`, image smoke and evidence recorder | Hosted CI, staging image digest, real OIDC/model/feed, multi-replica load, managed backups/PITR, agreed SLO/RTO/RPO and retention |

## Audit decision (not changed)

The current pilot requirement is **undecided**, not "every released answer is
durably audited." `docs/OBSERVABILITY.md` documents best-effort delivery and
`tests/security/test_audit_contract.py` pins it. A failed insert may release an
answer and increments `pac.persistence.failures{kind="audit"}`.

If durable audit is required, prefer committing the answer/turn/run outcome
and audit record in one PostgreSQL transaction before returning or replaying
it. A separate fail-closed audit write is simpler but can leave a committed
answer that replay must withhold; it needs explicit replay checks and handling
for uncertain commit acknowledgments. Both reduce availability during audit
storage failures. Neither guarantees that a client received a committed answer.

Proposed acceptance: make the audit insert fail, make commit acknowledgment
uncertain, restart a worker, and replay the same idempotency key. No fresh or
replayed answer may be released unless exactly one durable audit record and
its committed outcome exist. Restore storage and retry; the key must produce
one outcome without duplicated side effects. Verify permission changes before
replay, plus an alert on every refused audit write. This stronger contract
requires an explicit product decision before implementation.
