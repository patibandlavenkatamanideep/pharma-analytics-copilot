"""Telemetry: what may leave the process, and what happens when the
collector is not there.

No collector and no network: in-memory exporters stand in for the
collector, and a dead local port stands in for an outage.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from app import telemetry
from app.telemetry import (
    METRIC_LABELS, REDACTED, RedactingSpanExporter, _redacted_metrics, clean,
)

SECRETS = {
    "question": "show me revenue for Acme Midtown Infusion",
    "db.statement": "SELECT sum(wac) FROM sales WHERE org_id = 'FA001'",
    "enduser.id": "venkata@example.com",
    "pac.session": "dGhpcyBpcyBhIHNlc3Npb24gdG9rZW4gdmFsdWU",
}


@pytest.fixture
def spans():
    memory = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(memory)))
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    telemetry.use(provider, meters)
    yield SimpleNamespace(memory=memory, provider=provider, reader=reader, meters=meters)
    telemetry.reset()


def exported_strings(memory) -> list[str]:
    out = []
    for s in memory.get_finished_spans():
        out.append(s.name)
        out += [f"{k}={v}" for k, v in (s.attributes or {}).items()]
        for e in s.events:
            out.append(e.name)
            out += [f"{k}={v}" for k, v in (e.attributes or {}).items()]
        if s.status.description:
            out.append(s.status.description)
    return out


# -- the allowlist -------------------------------------------------------------

def test_unknown_attributes_are_dropped_and_odd_values_redacted():
    got = clean({"pac.status": "answered", "pac.row_count": 12, "pac.truncated": False,
                 "pac.metric": "x" * 200, "pac.reason": "it's; DROP TABLE",
                 "pac.role": ["exec"], **SECRETS})
    assert got == {"pac.status": "answered", "pac.row_count": 12, "pac.truncated": False,
                   "pac.metric": REDACTED, "pac.reason": REDACTED}


def test_identifiers_are_allowed_only_under_identifier_names():
    rid = "3f2a9c41b0d84e7f"
    assert clean({"pac.request_id": rid}) == {"pac.request_id": rid}
    # An id-shaped value under a label is still redacted if it is too long
    # for an enum; under a metric label it is not even allowed as a name.
    assert clean({"request_id": rid}, METRIC_LABELS, frozenset()) == {}


# -- redaction inside the exporter --------------------------------------------

def test_the_exporter_strips_what_the_call_site_did_not(spans):
    tracer = spans.provider.get_tracer("someone-else")
    with tracer.start_as_current_span("db.query", attributes=SECRETS) as s:
        s.set_attribute("pac.status", "answered")
        s.add_event("exception", {"exception.type": "UndefinedColumn",
                                  "exception.message": SECRETS["db.statement"]})
        s.set_status(Status(StatusCode.ERROR, SECRETS["db.statement"]))
    (exported,) = spans.memory.get_finished_spans()
    assert exported.name == "pac.span"
    assert dict(exported.attributes) == {"pac.status": "answered"}
    assert [dict(e.attributes) for e in exported.events] == [{"exception.type": "UndefinedColumn"}]
    assert exported.status.status_code == StatusCode.ERROR
    assert exported.status.description is None
    for text in exported_strings(spans.memory):
        for secret in SECRETS.values():
            assert secret not in text


def test_a_span_records_an_exception_by_type_not_message(spans):
    with pytest.raises(ValueError):
        with telemetry.span("pac.sql"):
            raise ValueError(SECRETS["db.statement"])
    (exported,) = spans.memory.get_finished_spans()
    assert exported.status.description == "ValueError"
    assert not exported.events


def test_expected_exceptions_are_outcomes_not_errors(spans):
    class Refused(Exception):
        pass

    with pytest.raises(Refused):
        with telemetry.span("pac.policy", expected=(Refused,)):
            raise Refused()
    (exported,) = spans.memory.get_finished_spans()
    assert exported.status.status_code != StatusCode.ERROR
    assert exported.attributes["pac.outcome"] == "Refused"


# -- metrics -------------------------------------------------------------------

def points(reader):
    data = reader.get_metrics_data()
    out = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for p in m.data.data_points:
                    out.append((m.name, dict(p.attributes)))
    return out


def test_metric_labels_outside_the_allowlist_are_dropped_at_record_time(spans):
    telemetry.count("pac.ask.outcomes", status="answered", role="ram",
                    request_id="3f2a9c41b0d84e7f", user_id="U009",
                    conversation_id="c_123")
    assert points(spans.reader) == [("pac.ask.outcomes", {"status": "answered", "role": "ram"})]


def test_the_metric_exporter_drops_points_that_carry_a_forbidden_label(spans):
    meter = spans.meters.get_meter("someone-else")
    counter = meter.create_counter("pac.ask.outcomes")
    counter.add(1, {"status": "answered"})
    counter.add(1, {"status": "answered", "user_id": "U009"})
    cleaned = _redacted_metrics(spans.reader.get_metrics_data())
    labels = [dict(p.attributes) for rm in cleaned.resource_metrics
              for sm in rm.scope_metrics for m in sm.metrics for p in m.data.data_points]
    assert labels == [{"status": "answered"}]


def test_recording_an_undeclared_metric_is_a_programming_error():
    with pytest.raises(KeyError):
        telemetry.count("pac.not_declared")


# -- the collector is down -----------------------------------------------------

class Exploding(SpanExporter):
    def export(self, spans):
        raise ConnectionError("collector unreachable")

    def shutdown(self):
        pass


class Stalled(SpanExporter):
    """A collector that accepts the connection and never answers."""

    def __init__(self):
        self.calls = 0

    def export(self, spans):
        self.calls += 1
        time.sleep(2)
        return SpanExportResult.FAILURE

    def shutdown(self):
        pass


def test_an_exporter_that_raises_never_reaches_the_caller():
    exporter = RedactingSpanExporter(Exploding())
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.use(provider, None)
    try:
        with telemetry.span("pac.ask"):
            pass                      # exported synchronously, and failed
    finally:
        telemetry.reset()
    assert exporter.export([]) == SpanExportResult.FAILURE


def test_a_stalled_collector_never_holds_up_the_code_being_traced():
    stalled = Stalled()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(stalled),
                                                   max_queue_size=256,
                                                   max_export_batch_size=64,
                                                   schedule_delay_millis=10))
    telemetry.use(provider, None)
    try:
        started = time.perf_counter()
        for _ in range(5000):          # far more than the queue holds
            with telemetry.span("pac.sql"):
                pass
        elapsed = time.perf_counter() - started
    finally:
        telemetry.reset()
    # 5000 spans against an exporter that takes 2 s per call: the spans were
    # queued or dropped, never waited for.
    assert elapsed < 1.5, elapsed
    assert stalled.calls >= 1


def test_an_unreachable_collector_is_survivable_end_to_end():
    settings = SimpleNamespace(otel_endpoint="http://127.0.0.1:9", otel_timeout_s=0.5,
                               release="test")
    telemetry.configure(settings)
    try:
        for _ in range(50):
            with telemetry.span("pac.ask", **{"pac.status": "answered"}):
                telemetry.count("pac.ask.outcomes", status="answered", role="exec")
    finally:
        started = time.perf_counter()
        telemetry.shutdown()          # flush attempts fail; shutdown still returns
        assert time.perf_counter() - started < 10


def test_no_collector_means_no_telemetry_and_no_cost():
    telemetry.reset()
    telemetry.configure(SimpleNamespace(otel_endpoint="", otel_timeout_s=1, release="dev"))
    started = time.perf_counter()
    for _ in range(10_000):
        with telemetry.span("pac.compile", **{"pac.metric": "paid_pack_units"}):
            telemetry.count("pac.ask.outcomes", status="answered", role="exec")
    per_call_us = (time.perf_counter() - started) / 10_000 * 1e6
    assert per_call_us < 200, per_call_us


# -- model calls -----------------------------------------------------------------

def test_each_model_call_is_a_span_and_tokens_are_counted_as_reported(spans, monkeypatch):
    from tests.unit.test_live_adapter_contract import (
        FakeBlock, FakeResponse, FakeUsage, context_with, make_planner, valid_plan_block,
    )

    planner = make_planner([
        FakeResponse([FakeBlock(type="tool_use", name="emit_plan",
                                input={"metric": "not_a_metric"})], FakeUsage(300, 30)),
        FakeResponse([valid_plan_block()], FakeUsage(100, 10)),
    ])
    monkeypatch.setattr(planner.settings, "llm_input_usd_per_mtok", 3.0)
    monkeypatch.setattr(planner.settings, "llm_output_usd_per_mtok", 15.0)
    planner.plan("top accounts", context_with("top accounts"))

    attempts = [s for s in spans.memory.get_finished_spans() if s.name == "pac.plan.attempt"]
    assert [(s.attributes["pac.attempt"], s.attributes["pac.outcome"]) for s in attempts] == [
        (1, "invalid_plan"), (2, "plan")]
    totals = {}
    for name, labels in points(spans.reader):
        totals.setdefault(name, []).append(labels)
    data = spans.reader.get_metrics_data()
    values = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for p in m.data.data_points:
                    values[(m.name, tuple(sorted(p.attributes.items())))] = p.value
    model = planner.model_id
    assert values[("pac.llm.tokens", (("direction", "input"), ("model", model)))] == 400
    assert values[("pac.llm.tokens", (("direction", "output"), ("model", model)))] == 40
    assert values[("pac.llm.cost", (("model", model),))] == pytest.approx(
        (400 * 3.0 + 40 * 15.0) / 1e6)


def test_a_call_with_no_reported_usage_is_unknown_not_free(spans):
    from tests.unit.test_live_adapter_contract import (
        FakeResponse, context_with, make_planner, valid_plan_block,
    )

    planner = make_planner([FakeResponse([valid_plan_block()], None)])
    planner.plan("top accounts", context_with("top accounts"))
    names = [name for name, _ in points(spans.reader)]
    assert "pac.llm.usage_unknown" in names
    assert "pac.llm.tokens" not in names


def test_these_tests_do_not_depend_on_the_ambient_sampler(monkeypatch):
    """A test environment may carry production sampling (1% is common). The
    providers built here pin ALWAYS_ON, so the spans they check exist
    whatever OTEL_TRACES_SAMPLER says; configure() still follows it."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    memory = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with provider.get_tracer("t").start_as_current_span("pac.check"):
        pass
    assert [s.name for s in memory.get_finished_spans()] == ["pac.check"]
    assert not TracerProvider().sampler.should_sample(None, 1, "x").decision.is_sampled(), \
        "an unpinned provider follows the environment, as production does"
