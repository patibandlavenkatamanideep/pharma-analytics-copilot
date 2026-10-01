"""Operational telemetry: traces and metrics in OpenTelemetry form, redacted
before they leave the process.

Two records, kept apart on purpose:

* The **security record** is the database: ``app_meta.query_audit`` (one row
  per request, written in its own transaction, keyed by request id), runs,
  sessions and login attempts. It is durable, and what it holds is chosen
  column by column: hashes, codes and counts, never text from a question or
  a result.
* **Telemetry** is operational and lossy by design. Spans and metrics are
  exported in the background through a bounded queue. When the collector is
  slow or down they are dropped. They never hold up a request, and their
  loss is never a security event.

Nothing protected is exported. Every span attribute and metric label passes
an allowlist of names. Values must be scalars, and strings must be short
and plain. Question text, SQL, result rows, account names, email addresses
and prices are on no allowlist. Redaction runs where the attribute is set
AND again inside the exporter, so an attribute added later -- by a library,
or by a change that forgot the rule -- is stripped before export rather
than trusted. Metric labels never carry user, conversation or request ids;
those are unbounded, and a label is not the place for an identifier.

With no collector configured (``PAC_OTEL_ENDPOINT`` unset) everything here
is a no-op.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# What may leave the process
# ---------------------------------------------------------------------------

#: Span attributes that may be exported. Identifiers of a request are allowed
#: on a span -- a trace IS one request -- but never on a metric.
SPAN_ATTRIBUTES = frozenset({
    # correlation and versions
    "pac.request_id", "pac.run_id", "pac.dataset_id", "pac.release",
    "pac.prompt_version", "pac.planner_version", "pac.model_id", "pac.provider",
    "pac.registry_version", "pac.policy_version", "pac.graph_version",
    # outcome
    "pac.role", "pac.status", "pac.persistence", "pac.turn_kind", "pac.outcome",
    "pac.reason", "pac.metric", "pac.replayed",
    # planning
    "pac.attempt", "pac.attempt_kind", "pac.attempts", "pac.repaired",
    "pac.tokens.input", "pac.tokens.output", "pac.usage_known",
    # execution
    "pac.row_count", "pac.truncated", "pac.db.pool",
    # ingestion
    "pac.ingest.source", "pac.ingest.status", "pac.ingest.applied",
    "pac.ingest.corrected", "pac.ingest.tombstoned", "pac.ingest.duplicates",
    "pac.ingest.quarantined", "pac.ingest.anchor_shift_weeks",
    # HTTP (semantic-convention names)
    "http.request.method", "http.route", "http.response.status_code",
    "pac.auth.outcome",
})

#: Attributes whose values are identifiers rather than short enums.
_ID_ATTRIBUTES = frozenset({"pac.request_id", "pac.run_id", "pac.dataset_id",
                            "pac.release", "pac.model_id"})

#: Metric label names. Every one has a small, bounded set of values.
METRIC_LABELS = frozenset({
    "status", "role", "persistence", "stage", "outcome", "kind", "model",
    "direction", "pool", "reason", "source", "http.route", "http.request.method",
    "http.response.status_class",
})

#: Event attributes that may be exported. An exception's message can quote
#: SQL or data; its type cannot.
_EVENT_ATTRIBUTES = frozenset({"exception.type"})
_RESOURCE_ATTRIBUTES = frozenset({"service.name", "service.version",
                                  "telemetry.sdk.name", "telemetry.sdk.language",
                                  "telemetry.sdk.version"})

_ID_VALUE = re.compile(r"^[A-Za-z0-9_.:\-]{1,96}$")
_ENUM_VALUE = re.compile(r"^[A-Za-z0-9_.:\-/{}]{0,48}$")
_SPAN_NAME = re.compile(r"^(pac|http)\.[a-z_.]{1,40}$")
REDACTED = "[redacted]"


def clean(attributes: dict[str, Any] | Any, allowed: frozenset[str] = SPAN_ATTRIBUTES,
          ids: frozenset[str] = _ID_ATTRIBUTES) -> dict[str, Any]:
    """The exportable subset of `attributes`. Unknown names are dropped;
    values that are not plain scalars are dropped or redacted."""
    out: dict[str, Any] = {}
    for key, value in dict(attributes or {}).items():
        if key not in allowed or value is None:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            pattern = _ID_VALUE if key in ids else _ENUM_VALUE
            out[key] = value if pattern.match(value) else REDACTED
        # Sequences, mappings and bytes are never exported.
    return out


# ---------------------------------------------------------------------------
# Exporters that redact
# ---------------------------------------------------------------------------

class RedactingSpanExporter(SpanExporter):
    """Wraps a real exporter. Rebuilds every span from the allowlists before
    handing it on, and never lets an export failure escape."""

    def __init__(self, inner: SpanExporter):
        self.inner = inner

    def export(self, spans) -> SpanExportResult:
        try:
            return self.inner.export([_redacted(s) for s in spans])
        except Exception as exc:     # a collector that is down is not our failure
            log.warning("telemetry export failed (%s); %d spans dropped",
                        type(exc).__name__, len(spans))
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self.inner.shutdown()
        except Exception:
            log.warning("telemetry exporter shutdown failed", exc_info=True)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return self.inner.force_flush(timeout_millis)
        except Exception:
            return False


def _redacted(span: ReadableSpan) -> ReadableSpan:
    status = span.status
    description = status.description
    if description and not _ENUM_VALUE.match(description):
        description = None
    resource = span.resource
    if resource is not None:
        resource = Resource({k: v for k, v in resource.attributes.items()
                             if k in _RESOURCE_ATTRIBUTES})
    return ReadableSpan(
        name=span.name if _SPAN_NAME.match(span.name or "") else "pac.span",
        context=span.context,
        parent=span.parent,
        resource=resource,
        attributes=clean(span.attributes),
        events=[Event(e.name if _SPAN_NAME.match(e.name or "") or e.name == "exception"
                      else "pac.event",
                      attributes=clean(e.attributes, _EVENT_ATTRIBUTES, frozenset()),
                      timestamp=e.timestamp)
                for e in span.events],
        links=(),
        kind=span.kind,
        status=Status(status.status_code, description),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingMetricExporter(MetricExporter):
    """Wraps a real metric exporter. A data point carrying a label outside
    the allowlist is dropped whole -- relabelling it would merge it into a
    series it does not belong to. Exemplars, which carry trace ids and
    unaggregated attributes, are removed."""

    def __init__(self, inner: MetricExporter):
        super().__init__(preferred_temporality=getattr(inner, "_preferred_temporality", None),
                         preferred_aggregation=getattr(inner, "_preferred_aggregation", None))
        self.inner = inner

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        try:
            return self.inner.export(_redacted_metrics(metrics_data),
                                     timeout_millis=timeout_millis, **kwargs)
        except Exception as exc:
            log.warning("metric export failed (%s); interval dropped", type(exc).__name__)
            return MetricExportResult.FAILURE

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        try:
            return self.inner.force_flush(timeout_millis=timeout_millis)
        except Exception:
            return False

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        try:
            self.inner.shutdown(timeout_millis=timeout_millis, **kwargs)
        except Exception:
            log.warning("metric exporter shutdown failed", exc_info=True)


def _allowed_point(point) -> bool:
    return set(point.attributes or {}) <= METRIC_LABELS and all(
        isinstance(v, (str, bool, int, float)) and (not isinstance(v, str)
                                                     or _ENUM_VALUE.match(v))
        for v in (point.attributes or {}).values())


def _redacted_metrics(data):
    resources = []
    for rm in data.resource_metrics:
        scopes = []
        for sm in rm.scope_metrics:
            kept = []
            for metric in sm.metrics:
                points = [dataclasses.replace(p, exemplars=[])
                          for p in metric.data.data_points if _allowed_point(p)]
                if len(points) != len(metric.data.data_points):
                    log.error("metric %s carried a label outside the allowlist; "
                              "points dropped", metric.name)
                kept.append(dataclasses.replace(
                    metric, data=dataclasses.replace(metric.data, data_points=points)))
            scopes.append(dataclasses.replace(sm, metrics=kept))
        resources.append(dataclasses.replace(rm, scope_metrics=scopes))
    return dataclasses.replace(data, resource_metrics=resources)


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------

#: name -> (kind, unit, description). Recording a name not listed here is a
#: programming error, caught by the tests rather than shipped.
INSTRUMENTS: dict[str, tuple[str, str, str]] = {
    "pac.http.server.requests": ("counter", "{request}", "HTTP requests by route template"),
    "pac.http.server.duration": ("histogram", "ms", "HTTP request latency by route template"),
    "pac.ask.outcomes": ("counter", "{request}", "Questions by outcome, role and persistence"),
    "pac.ask.duration": ("histogram", "ms", "Question latency by outcome"),
    "pac.stage.duration": ("histogram", "ms", "Latency of each request stage"),
    "pac.llm.attempts": ("counter", "{attempt}", "Model calls by outcome"),
    "pac.llm.tokens": ("counter", "{token}", "Provider-reported tokens"),
    "pac.llm.usage_unknown": ("counter", "{attempt}", "Model calls whose usage was not reported"),
    "pac.llm.cost": ("counter", "USD", "Estimated model cost at the configured rates"),
    "pac.db.pool.wait": ("histogram", "ms", "Time waiting for a pooled connection"),
    "pac.db.pool.timeouts": ("counter", "{event}", "Requests that found no connection in time"),
    "pac.db.errors": ("counter", "{event}", "Analytical queries that failed, by kind"),
    "pac.persistence.failures": ("counter", "{event}", "Turns or audit rows not recorded"),
    "pac.ingest.batches": ("counter", "{batch}", "Ingestion batches by outcome"),
    "pac.ingest.events": ("counter", "{event}", "Ingested events by outcome"),
    "pac.ingest.quarantined": ("counter", "{event}", "Quarantined events by reason"),
    "pac.ingest.lag": ("gauge", "s", "Age of the newest applied event when its batch landed"),
    "pac.ingest.duration": ("histogram", "s", "Ingestion batch duration"),
    "pac.admission.refused": ("counter", "{request}", "Work refused for load, by stage and reason"),
    "pac.admission.wait": ("histogram", "ms", "Time queued for admission, by stage"),
}

#: Spans whose duration is also a stage metric.
STAGES = frozenset({"pac.state_load", "pac.resolve", "pac.plan", "pac.check",
                    "pac.policy", "pac.answer", "pac.compile", "pac.validate",
                    "pac.sql", "pac.render", "pac.finalise", "pac.audit", "pac.auth"})


@dataclass
class _State:
    tracer: Any = field(default_factory=lambda: trace.NoOpTracer())
    instruments: dict[str, Any] = field(default_factory=dict)
    providers: list[Any] = field(default_factory=list)
    configured: bool = False


_state = _State()


def _make_instruments(meter) -> dict[str, Any]:
    made = {}
    for name, (kind, unit, description) in INSTRUMENTS.items():
        factory = {"counter": meter.create_counter, "histogram": meter.create_histogram,
                   "gauge": meter.create_gauge}[kind]
        made[name] = factory(name, unit=unit, description=description)
    return made


_state.instruments = _make_instruments(metrics.NoOpMeter("pac"))


def use(tracer_provider=None, meter_provider=None) -> None:
    """Point this module at the given providers (tests, or configure())."""
    _state.tracer = (tracer_provider.get_tracer("pac") if tracer_provider
                     else trace.NoOpTracer())
    _state.instruments = _make_instruments(
        meter_provider.get_meter("pac") if meter_provider else metrics.NoOpMeter("pac"))


def reset() -> None:
    use(None, None)
    _state.configured = False


def configure(settings) -> None:
    """Install exporters to the configured OTLP/HTTP collector, if any.

    Spans: a BatchSpanProcessor with a bounded queue -- a span that finds the
    queue full is dropped, never waited for. Metrics: a periodic reader.
    Both go through the redacting exporters above.
    """
    if _state.configured or not settings.otel_endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    base = settings.otel_endpoint.rstrip("/")
    resource = Resource({"service.name": "pharma-analytics-copilot",
                         "service.version": settings.release})
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    tracer_provider.add_span_processor(BatchSpanProcessor(
        RedactingSpanExporter(OTLPSpanExporter(endpoint=f"{base}/v1/traces",
                                               timeout=settings.otel_timeout_s)),
        max_queue_size=2048, schedule_delay_millis=2000, max_export_batch_size=512,
        export_timeout_millis=settings.otel_timeout_s * 1000))
    meter_provider = MeterProvider(
        resource=resource, shutdown_on_exit=False,
        metric_readers=[PeriodicExportingMetricReader(
            RedactingMetricExporter(OTLPMetricExporter(endpoint=f"{base}/v1/metrics",
                                                       timeout=settings.otel_timeout_s)),
            export_interval_millis=15_000,
            export_timeout_millis=settings.otel_timeout_s * 1000)])
    use(tracer_provider, meter_provider)
    _state.providers = [tracer_provider, meter_provider]
    _state.configured = True
    log.info("telemetry exporting to %s", base)


def shutdown() -> None:
    """Flush what can be flushed within the exporters' timeouts, then stop."""
    for provider in _state.providers:
        try:
            provider.shutdown()
        except Exception:
            log.warning("telemetry shutdown failed", exc_info=True)
    _state.providers = []
    reset()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

class SpanHandle:
    __slots__ = ("_span",)

    def __init__(self, span):
        self._span = span

    def set(self, **attributes: Any) -> None:
        try:
            self._span.set_attributes(clean(attributes))
        except Exception:
            log.debug("span attribute not recorded", exc_info=True)

    @property
    def context(self):
        """A context in which this span is the parent -- for work that runs
        where the current context does not follow (another thread)."""
        return trace.set_span_in_context(self._span)


@contextmanager
def span(name: str, *, parent=None, expected: tuple[type[BaseException], ...] = (),
         **attributes: Any) -> Iterator[SpanHandle]:
    """A span named `name`, child of `parent` (a context) or of the current
    span. An exception marks it as an error by TYPE only: messages can quote
    SQL or data. Exceptions in `expected` are control flow, not failures."""
    started = time.perf_counter()
    token = otel_context.attach(parent) if parent is not None else None
    try:
        with _state.tracer.start_as_current_span(
                name, attributes=clean(attributes), record_exception=False,
                set_status_on_exception=False) as s:
            try:
                yield SpanHandle(s)
            except expected as exc:
                s.set_attributes(clean({"pac.outcome": type(exc).__name__}))
                raise
            except BaseException as exc:
                s.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
    finally:
        if token is not None:
            otel_context.detach(token)
        if name in STAGES:
            observe("pac.stage.duration", (time.perf_counter() - started) * 1000, stage=name)


def annotate(**attributes: Any) -> None:
    """Set attributes on the current span, through the allowlist."""
    try:
        trace.get_current_span().set_attributes(clean(attributes))
    except Exception:
        log.debug("span attribute not recorded", exc_info=True)


def current():
    """The current context, to parent work that may run on another thread."""
    return otel_context.get_current()


def _record(name: str, method: str, value: float, labels: dict[str, Any]) -> None:
    try:
        getattr(_state.instruments[name], method)(value, clean(labels, METRIC_LABELS,
                                                               frozenset()))
    except KeyError:
        raise
    except Exception:
        log.debug("metric %s not recorded", name, exc_info=True)


def count(name: str, value: float = 1, **labels: Any) -> None:
    _record(name, "add", value, labels)


def observe(name: str, value: float, **labels: Any) -> None:
    _record(name, "record", value, labels)


def gauge(name: str, value: float, **labels: Any) -> None:
    _record(name, "set", value, labels)


def status_class(code: int) -> str:
    return f"{code // 100}xx"
