# Operations dashboard and feedback review

This is a dashboard specification (as Prometheus queries in [deploy/observability/dashboard.json](../deploy/observability/dashboard.json), each run against a local Prometheus by `scripts/ops_drill.py`) for the existing OpenTelemetry
metrics and PostgreSQL audit/feedback records. No second SDK is introduced.
It is prepared for staging, not a claim that a hosted dashboard exists.

Use a 15-minute operational window and a 24-hour review window, with release
and environment selected by resource metadata. Never use a question, SQL,
email, account, territory, result value, conversation, request or run identifier
as a metric label. Keep model and source values to an operator-controlled
inventory. View missing series as **unknown**, not zero. Ratios with no events
are unavailable; show their event counts alongside them.

| Panel | Existing source and aggregation | Interpretation / action |
|---|---|---|
| Answers, clarifications, denials, errors | `pac.ask.outcomes`: rate and count grouped by `status`, optional `role`; divide each by all outcomes | Clarification and denial are valid outcomes. Keep them separate from errors and overload |
| Answer latency | p50/p95 of `pac.ask.duration`, group by `status`; stage p95 from `pac.stage.duration` | Keep success and refusal latency separate. SLO thresholds need agreement |
| HTTP failures | `pac.http.server.requests` by route template and status class | Includes failures before pipeline entry; do not sum HTTP and ask counts as if they were distinct requests |
| Model attempts and repairs | `pac.llm.attempts` by `kind`, `outcome`, `model`; repair-attempt count / all attempts | This is attempt frequency, not fraction of questions repaired. Use the audit aggregate below for request-level repair |
| Tokens and spend | `pac.llm.tokens` by direction/model, `pac.llm.cost`, plus `pac.llm.usage_unknown` | Costs only exist with configured contracted rates. Unknown usage never becomes zero spend. Show configured rates and provider invoice reconciliation separately |
| Offline answers / fallback | Aggregate `query_audit.model_id` and `planner_repaired` for answered rows | The configured offline planner is a service mode, not an automatic provider fallback. Do not invent a fallback-success metric |
| Admission pressure | `pac.admission.refused` by stage/reason; p95 `pac.admission.wait`; pool wait/timeouts | Existing instrumentation measures refusals and waits, not instantaneous queue occupancy. Show configured slots × workers × replicas beside this panel |
| Permissions | `pac.ask.outcomes{status="denied"}` and bounded HTTP 403 counts | Never label with the denied place, question or user. An unexpected spike calls for security review, not policy relaxation |
| Audit and turn persistence | `pac.persistence.failures` by `kind` | Page on any audit loss under the present best-effort contract. A telemetry outage can itself hide loss; reconcile with sanitized logs |
| Feed freshness | `pac.ingest.since_success`, `pac.ingest.watermark_age` by source, plus absent-series check | Thresholds follow the expected feed schedule and allowable data lag. These gauges continue aging without another batch |
| Feed quality and runtime | `pac.ingest.rejected_since_success` and `pac.ingest.quarantined_last_day` by source (read from the batch log by the serving processes), duration | The jobs process is one-shot: its own batch, event and quarantine counters are exported once per run and expire. Use the persisted batch record for per-run reconciliation |
| User feedback | Aggregated rating/reason counts from `app_conv.feedback` | Feedback is self-selected, may be replaced, and is not an accuracy score. Never export comments or identity-bearing rows |

## Request-level review queries

Run only through an authorized operations connection. These aggregate existing
records; do not give the serving role additional privileges for dashboards.
Return counts only, without identifiers, questions, SQL or feedback comments.

```sql
SELECT status, count(*) AS requests,
       count(*) FILTER (WHERE planner_repaired IS TRUE) AS repaired,
       count(*) FILTER (WHERE usage_known IS FALSE) AS unknown_usage
FROM app_meta.query_audit
WHERE created_at >= now() - interval '24 hours'
GROUP BY status;

SELECT rating, reason, count(*) AS responses
FROM app_conv.feedback
WHERE created_at >= now() - interval '24 hours'
GROUP BY rating, reason;

SELECT model_id, count(*) AS answered
FROM app_meta.query_audit
WHERE created_at >= now() - interval '24 hours' AND status = 'answered'
GROUP BY model_id;
```

Audit aggregates omit failed audit writes. Display that caveat with the audit
loss panel; they cannot establish complete request counts under best effort.
Review negative feedback and failures with `scripts/failure_sample.py` under
its access controls. Reproduce a reported defect with synthetic data, have a
human verify the expected metric/scope/period, add a regression, then rerun
the affected semantic and security gates. Feedback never changes permissions
or automatically trains/tunes a prompt. Previously inspected evaluation sets
remain regression sets.

## Staging acceptance

Load the alert proposals from `OBSERVABILITY.md`, confirming the backend's
actual metric name/unit translation first. Exercise one successful answer,
clarification, denial, repair, rate limit, overload, failed audit insert,
malformed batch and stopped feed. Verify each panel's count and alert against
the corresponding sanitized local evidence. Stop/slow the collector and
confirm answers remain available, export buffering stays bounded and missing
telemetry is visible. Restore it and verify alerts clear. Record the actual
image digest, backend configuration and timings in the staging evidence.

The local telemetry suites already cover exporter exceptions, a stalled
collector, redaction and stopped-feed freshness. Repeat them against the final
candidate, but only a real hosted collector and incident drill can close the
staging delivery/alerting gate. Agree SLOs, alert owners, retention, RTO/RPO and
backup/PITR before using this dashboard to claim production readiness.
