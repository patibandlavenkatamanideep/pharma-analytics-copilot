# Observability

What the running system reports about itself, where that goes, and what it
is never allowed to contain. The code is
[app/telemetry.py](../app/telemetry.py). The tests are
[tests/unit/test_telemetry.py](../tests/unit/test_telemetry.py) and
[tests/integration/test_telemetry_pipeline.py](../tests/integration/test_telemetry_pipeline.py).

## Two records, kept apart

| | Security record | Operational telemetry |
|---|---|---|
| Where | PostgreSQL: `app_meta.query_audit`, `app_conv.runs`, sessions, login attempts | OpenTelemetry traces and metrics, sent to an OTLP collector |
| Delivery | Written in a transaction; the audit row is keyed by request id, so a replayed step cannot write it twice | Exported in the background through a bounded queue; dropped when the collector is slow or down |
| Contents | Chosen column by column: hashes, codes, counts, versions, timings | Allowlisted attribute names, short scalar values |
| If it fails | A failed audit write is logged and counted (`pac.persistence.failures{kind="audit"}`); the answer is still returned. A failed turn commit returns the answer marked `persistence: failed` | Spans and points are lost. The request is unaffected. That loss is not a security event |

Telemetry never stands in for the audit trail. The audit trail does not
depend on telemetry.

### Audit durability: the current policy, and the decision it needs

**Current policy: best effort.** The audit row is written in its own short
transaction before the response is sent. If that write fails, because the
database refuses it or is unreachable at that moment, the answer is **still
returned**. The failure is logged and counted, and the "Audit loss" alert
below pages on the first one. A released answer can therefore have no audit
row. This is pinned by a test, so it cannot change by accident: the
database refuses the insert, the answer is returned, one failure is
counted and no row exists
(`tests/security/test_audit_contract.py::test_the_current_policy_a_failed_audit_write_is_counted_and_the_answer_still_returned`).
Sampled telemetry does not substitute for the missing row.

**Not decided:** whether that satisfies the audit requirement. The
assignment asks for access control, not an audit regime. If the product or
compliance owner requires durable audit evidence for **every released
answer**, one of these replaces the current policy:

| Option | What changes | Cost |
|---|---|---|
| Fail closed | A failed audit write withholds the answer (`503`, retryable under the same idempotency key) | An audit-table problem becomes an outage for answers |
| One transaction | The audit row is written in the transaction that commits the turn and stores the run's outcome, so either both exist or neither does | Couples the audit write to turn persistence, and needs care for refusals and clarifications, which commit no turn |

Either is a contained change in `Pipeline` / `state.finalise`. The test
above is the one that would change with it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PAC_OTEL_ENDPOINT` | unset | Base URL of an OTLP/HTTP collector, e.g. `http://otel-collector:4318`. Unset, nothing is exported and recording is a no-op (measured below 200 µs per span plus metric, in a loop of 10,000) |
| `PAC_OTEL_TIMEOUT_S` | `5` | Upper bound on each export call. It runs in the background, never on a request |
| `PAC_RELEASE` | `dev` | Reported as `service.version` and `pac.release`. Set by the build |
| `PAC_LLM_INPUT_USD_PER_MTOK`, `PAC_LLM_OUTPUT_USD_PER_MTOK` | unset | Contracted model rates. Unset, no cost is estimated, because an invented price would read as a measurement |

LangSmith stays off unless `PAC_LANGSMITH_TRACING=true`. It is not used.

## Traces

One trace per HTTP request. The spans form a tree, and every one of them
carries the request's trace id:

```
http.server                    route template, method, status
└─ pac.auth                    outcome (ok / rotated / absent / rejected), role
└─ pac.ask                     request id, run id, role, status, persistence,
   │                           dataset id, release, registry/policy/graph versions
   ├─ pac.state_load           conversation and run lease
   ├─ pac.resolve              continuity and entities
   ├─ pac.record_question / pac.await_reply   (clarifications)
   ├─ pac.plan                 provider, model, prompt and planner versions,
   │  └─ pac.plan.attempt      attempts, repaired, tokens, usage known
   │                           -- one per model call: ordinal, kind, outcome, tokens
   ├─ pac.check                metric, turn kind
   │  └─ pac.policy            outcome on refusal
   ├─ pac.answer
   │  ├─ pac.compile
   │  ├─ pac.validate
   │  ├─ pac.sql               row count
   │  └─ pac.render            truncated
   ├─ pac.finalise
   └─ pac.audit
pac.ingest                     source, status, applied, corrected, tombstoned,
                               duplicates, quarantined, anchor shift, dataset id
```

Graph nodes take their parent from the request rather than from the current
thread, so the tree holds whichever thread LangGraph runs a node on.

An exception marks a span as an error **by type only**. A message can quote
SQL or data, so it is never recorded. A refusal (`AuthorizationError`, an
unsupported combination, a clarification interrupt, a busy conversation) is
an outcome, not an error: it is recorded as `pac.outcome` and the span's
status is not set to error.

## Metrics

| Metric | Kind | Labels |
|---|---|---|
| `pac.http.server.requests` | counter | `http.route` (template), `http.request.method`, `http.response.status_class` |
| `pac.http.server.duration` | histogram, ms | `http.route` |
| `pac.ask.outcomes` | counter | `status`, `role`, `persistence` |
| `pac.ask.duration` | histogram, ms | `status` |
| `pac.stage.duration` | histogram, ms | `stage` |
| `pac.llm.attempts` | counter | `outcome`, `model`, `kind` |
| `pac.llm.tokens` | counter | `direction`, `model`. Only as the provider reported them |
| `pac.llm.usage_unknown` | counter | `model`. A call whose usage was not reported is counted here, never as zero |
| `pac.llm.cost` | counter, USD | `model`. Only with configured rates |
| `pac.db.pool.wait` | histogram, ms | `pool` (`exec`, `scoped`, `auth`) |
| `pac.db.pool.timeouts` | counter | `pool` |
| `pac.db.errors` | counter | `kind` (`timeout`, `unavailable`, `generation_changed`) |
| `pac.persistence.failures` | counter | `kind` (`turn`, `audit`) |
| `pac.ingest.batches` | counter | `status`, `source` |
| `pac.ingest.events` | counter | `outcome`, `source` |
| `pac.ingest.quarantined` | counter | `reason`, `source` |
| `pac.ingest.lag` | gauge, s | `source`. Arrival lag: age of the newest applied event when its batch landed. A diagnostic, **not** freshness: it does not change between batches |
| `pac.ingest.since_success` | observed gauge, s | `source`. Seconds since the source's last accepted batch, **read from the database at every collection**, so it grows when a feed stops |
| `pac.ingest.watermark_age` | observed gauge, s | `source`. Seconds since the newest event time applied for the source, read at every collection. Grows when the data stops moving forward, even if batches keep arriving |
| `pac.ingest.duration` | histogram, s | `status` |

Every label has a small, bounded set of values. **No metric carries a user,
conversation, request or run id.** Those are unbounded, and they belong on a
trace, not on a label. Recording an undeclared metric raises, so a new
metric must be declared with its labels.

## Redaction

Nothing protected is exported: no question text, SQL, result rows, account
or territory names, email addresses, prices, session tokens or credentials.
Three layers enforce this:

1. **Allowlisted names.** Span attributes and metric labels outside the
   allowlists are dropped where they are set.
2. **Plain values.** Strings must be short, with no spaces, punctuation
   limited to `_ . : - / { }`, and at most 48 characters (96 for the
   identifier attributes). Anything else is replaced with `[redacted]`.
   Sequences and mappings are never exported.
3. **The exporters redact again.** `RedactingSpanExporter` rebuilds every
   span from the allowlists before handing it to the real exporter: unknown
   span names, attributes, event attributes other than `exception.type`,
   links and unsafe status descriptions are all removed. An attribute set by
   a library, or by code that forgot the rule, is therefore stripped rather
   than trusted. `RedactingMetricExporter` drops any data point whose labels
   fall outside the allowlist, without relabelling it into another series,
   and strips exemplars.

The tests check this from the outside. A real exec question about pricing
and a real RAM question are run through the pipeline. Then every exported
span name, attribute, event, status and metric label is scanned for the
question text, both users' emails and ids, the RAM's territory, both
headlines, every text cell of the RAM's table, and SQL fragments. To check
that the scan catches leaks, redaction was disabled and a leak injected: 5
tests failed.

## When the collector is down

The `BatchSpanProcessor` queue holds 2,048 spans. A span that finds it full
is dropped. Exports run on a background thread, and each is bounded by
`PAC_OTEL_TIMEOUT_S`. Tested:

- an exporter that raises: the request completes, and the exporter wrapper
  returns failure without raising;
- an exporter that takes 2 s per call: 5,000 spans are recorded in under
  1.5 s, queued or dropped, never waited for. Through the pipeline, an
  answer with a stalled collector takes less than its untraced time plus 1 s;
- a real OTLP exporter pointed at a closed port: 50 traced operations, then
  shutdown, which returns in a bounded time.

On shutdown the app flushes what it can within those timeouts, then stops.
The whole flush is also capped at twice `PAC_OTEL_TIMEOUT_S` plus a second,
as a backstop. Each exporter request is already bounded by its own timeout.

**The ingestion job is a separate process** and exports its own telemetry.
`scripts/ingest.py` configures the exporters at start and flushes them in
a `finally`, after publication has committed. Tested with the real command
as a subprocess (`tests/integration/test_ingest_observability.py`):

- with a local OTLP receiver, `pac.ingest.batches`, `pac.ingest.duration`
  and the `pac.ingest` span arrive;
- with a collector that accepts and never answers, the batch is published
  and the command exits 0 in about 5 s. With nothing listening it takes
  about 2.6 s. The exporter timeout was 2 s in both cases.

## Freshness

A stopped feed has to be visible without another batch. The persisted
watermark (`app_ingest.watermarks`, written in the publishing transaction)
is read **at every metric collection** and turned into two gauges:
`pac.ingest.since_success` (time since the last accepted batch, which
catches missed runs) and `pac.ingest.watermark_age` (age of the newest
event applied, which catches data that stops moving). Both use the
database's clock. Publication delay is a third, separate measurement
(`pac.ingest.duration`). A running API exports the gauges continuously, and
the ingestion job exports them once per run. If they cannot be read, they
are omitted rather than reported wrong, and the absence alert above fires.

Independently of any collector, a scheduler can run:

```bash
python3 scripts/ingest.py --check-freshness --max-since-success 26h \
    --max-watermark-age 3d --source distributor-feed
```

It prints each source's freshness as JSON and exits 3 with stable codes
(`missed_run`, `stale_data`, `never_delivered`) when a limit is exceeded.
Tested with a feed that stops after a healthy batch. Moving the persisted
times back three days, with no new batch, raises the gauge by three days
and makes the check fail.

## Logs

One JSON object per line on stderr (`app/logs.py`). The image starts
uvicorn with `app/log_config.json`, so the first line is already JSON. The
same rules as spans apply: what code wrote is kept; what data or a client
supplied is not.

| Field | Holds |
|---|---|
| `event` | The message **template** as written in code (`"query failed (%s): %s"`), never the interpolated text |
| `args` | Each argument if it is a number, boolean, null or identifier-shaped text (a route, a model id, a run id), otherwise `"[redacted]"`. An exception, a question, SQL or an address never appears |
| `error` | The exception's type, its stable `code` if it has one, and the innermost frame in `app/` (`app/pipeline.py:1025`). Never its message or traceback |
| `http_id` | Generated per HTTP request and returned as `X-Request-ID`. A client cannot choose it |
| `request_id`, `run_id` | The audit row and run of the turn being served, from the moment the turn starts, including inside graph steps |
| access lines | `{"event": "http.access", "method", "path", "status"}`, with no query string (an OIDC callback carries its code and state there) and no client address |

`PAC_LOG_FORMAT=text` restores Python's default formatting for local work.
That formatting prints exception text, so it is not for a shared
environment.

Tested: `tests/security/test_log_hygiene.py` runs uvicorn's logging
configuration and the application's real startup in a subprocess, emits
records shaped like the code's own with a marker in each, and finds no
marker, no address and no non-JSON line in what the process writes. On the
unmodified code, all three leaked (`r3-logs-reproduced.json`). The same file
shows a warning raised inside a graph step carrying the response's
`X-Request-ID`, `request_id` and `run_id`. `tests/unit/test_logs.py` covers
the formatter.

## Alerts

These are proposed rules. They are written in Prometheus form, using the
OpenTelemetry-to-Prometheus name translation (`pac.ask.duration` in ms
becomes `pac_ask_duration_milliseconds_*`). **No collector or Prometheus has
been run against them.** The thresholds are starting points, not agreed
SLOs.

| Alert | Expression | For | Why |
|---|---|---|---|
| Slow answers | `histogram_quantile(0.95, sum by (le) (rate(pac_ask_duration_milliseconds_bucket[10m]))) > 10000` | 10m | p95 above 10 s |
| Errors | `sum(rate(pac_ask_outcomes_total{status="error"}[10m])) / sum(rate(pac_ask_outcomes_total[10m])) > 0.05` | 10m | More than 5% of questions fail |
| Model failing | `sum(rate(pac_llm_attempts_total{outcome!="plan"}[15m])) / sum(rate(pac_llm_attempts_total[15m])) > 0.2` | 15m | Transport errors or invalid plans |
| Usage not reported | `rate(pac_llm_usage_unknown_total[1h]) > 0` | 1h | Spend is not being measured |
| Pool saturation | `histogram_quantile(0.95, sum by (le, pool) (rate(pac_db_pool_wait_milliseconds_bucket[5m]))) > 500` | 5m | Requests queue for connections |
| Pool exhausted | `increase(pac_db_pool_timeouts_total[5m]) > 0` | — | A request found no connection |
| Database errors | `increase(pac_db_errors_total{kind="unavailable"}[5m]) > 0` | — | Database unreachable |
| Audit loss | `increase(pac_persistence_failures_total{kind="audit"}[5m]) > 0` | — | A request went unaudited. Page |
| Turns not saved | `increase(pac_persistence_failures_total{kind="turn"}[15m]) > 3` | — | Conversations not being recorded |
| Missed ingestion run | `max by (source) (pac_ingest_since_success_seconds) > 93600` | 15m | No accepted batch for 26 hours (a daily feed plus a margin; set to the feed's schedule) |
| Data not moving | `max by (source) (pac_ingest_watermark_age_seconds) > 259200` | 1h | Newest applied event older than three days, even if batches arrive |
| Freshness not reported | `absent_over_time(pac_ingest_since_success_seconds[30m])` | — | No process is reporting freshness: the API is down, the collector is not receiving, or the database cannot be read. Without this, the two alerts above would go quiet exactly when they are needed |
| Batch rejected | `increase(pac_ingest_batches_total{status="rejected"}[1h]) > 0` | — | A feed delivered a broken batch |
| Quarantine rising | `sum(increase(pac_ingest_quarantined_total[1d])) > 100` | — | Feed quality degrading |

## Not covered

- The deployed host exports nothing until a collector is provisioned and
  `PAC_OTEL_ENDPOINT` is set. No collector exists in this repository's
  compose files, because no requirement names one.
- Logs are written to stderr for the platform to collect. Shipping them
  to a log store is the platform's job, and none is configured here.

## Dashboard and feedback acceptance

[OPERATIONS_DASHBOARD.md](OPERATIONS_DASHBOARD.md) maps the existing metrics
and aggregate audit/feedback queries to concrete panels, explains unavailable
queue/fallback measurements, and defines the hosted delivery and incident
checks. It also preserves the distinction between best-effort audit and
operational telemetry.
