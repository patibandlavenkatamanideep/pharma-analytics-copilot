"""A real request, traced: every stage appears, correlated with the request,
and nothing protected is exported.

Runs the pipeline against PostgreSQL with in-memory exporters behind the
same redacting exporter a collector would sit behind.
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

from app import telemetry
from app.telemetry import METRIC_LABELS, RedactingSpanExporter

STAGES = ["pac.ask", "pac.state_load", "pac.resolve", "pac.plan", "pac.check",
          "pac.policy", "pac.answer", "pac.compile", "pac.validate", "pac.sql",
          "pac.render", "pac.finalise", "pac.audit"]


@pytest.fixture
def traced():
    memory = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(memory)))
    reader = InMemoryMetricReader()
    telemetry.use(provider, MeterProvider(metric_readers=[reader]))
    yield SimpleNamespace(memory=memory, reader=reader)
    telemetry.reset()


def everything_exported(memory, reader) -> list[str]:
    out = []
    for s in memory.get_finished_spans():
        out.append(s.name)
        out += [str(v) for v in (s.attributes or {}).values()]
        for e in s.events:
            out += [str(v) for v in (e.attributes or {}).values()]
        out.append(s.status.description or "")
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for p in m.data.data_points:
                    out += [str(v) for v in p.attributes.values()]
    return out


def test_an_answered_question_traces_every_stage_under_one_request(traced, pipeline, exec_user):
    result = pipeline.ask(exec_user, "total paid pack units last quarter")
    assert result.status == "answered"

    spans = traced.memory.get_finished_spans()
    by_name = {}
    for s in spans:
        by_name.setdefault(s.name, s)
    assert [n for n in STAGES if n not in by_name] == []
    ask = by_name["pac.ask"]
    assert {s.context.trace_id for s in spans if s.name in STAGES} == {ask.context.trace_id}
    assert by_name["pac.compile"].parent.span_id == by_name["pac.answer"].context.span_id
    assert ask.attributes["pac.request_id"] == result.request_id
    assert ask.attributes["pac.run_id"] == result.run_id
    assert ask.attributes["pac.status"] == "answered"
    assert ask.attributes["pac.persistence"] == "saved"
    assert ask.attributes["pac.dataset_id"] == pipeline.current_dataset()["dataset_id"]
    assert ask.attributes["pac.registry_version"] and ask.attributes["pac.policy_version"]
    assert by_name["pac.plan"].attributes["pac.provider"] == "offline"
    assert by_name["pac.check"].attributes["pac.metric"] == "paid_pack_units"
    assert by_name["pac.sql"].attributes["pac.row_count"] == 1


def test_nothing_protected_leaves_the_process(traced, pipeline, exec_user, ram_user):
    exec_answer = pipeline.ask(exec_user, "total WAC revenue last quarter", include_sql=True)
    ram_answer = pipeline.ask(ram_user, "top 5 accounts by paid pack units last quarter",
                              include_sql=True)
    assert exec_answer.status == "answered" and ram_answer.status == "answered"

    exported = "\n".join(everything_exported(traced.memory, traced.reader))
    forbidden = [
        "total WAC revenue last quarter", "top 5 accounts",      # question text
        exec_user.email, ram_user.email, exec_user.user_id,      # identity
        ram_user.scope_value,                                    # scope
        exec_answer.answer.headline, ram_answer.answer.headline,  # results
        "SELECT", "FROM sales", "app.scope",                     # SQL
    ]
    # Account names and every other text cell of the RAM's table.
    forbidden += [v for row in ram_answer.answer.table for v in row.values()
                  if isinstance(v, str) and any(c.isalpha() for c in v)]
    for value in forbidden:
        assert value and value not in exported, value


def test_metric_labels_are_bounded(traced, pipeline, exec_user, ram_user):
    for user, q in ((exec_user, "total paid pack units last quarter"),
                    (ram_user, "total paid pack units last quarter")):
        result = pipeline.ask(user, q)
    data = traced.reader.get_metrics_data()
    seen = set()
    ids = {result.request_id, result.run_id, result.conversation_id,
           exec_user.user_id, ram_user.user_id}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for p in m.data.data_points:
                    assert set(p.attributes) <= METRIC_LABELS, (m.name, p.attributes)
                    assert not ids & set(map(str, p.attributes.values())), p.attributes
                    seen.add(m.name)
    assert {"pac.ask.outcomes", "pac.ask.duration", "pac.stage.duration",
            "pac.db.pool.wait"} <= seen


def test_a_refusal_is_traced_as_an_outcome(traced, ram_user):
    from tests.hostile import pipeline_with

    # A planner persuaded to emit pricing for a RAM.
    hostile, _ = pipeline_with({"metric": "wac_revenue",
                                "time": {"kind": "named", "named": "last_quarter"}})
    result = hostile.ask(ram_user, "total WAC revenue last quarter")
    assert result.status == "denied"
    policy = [s for s in traced.memory.get_finished_spans() if s.name == "pac.policy"][0]
    assert policy.attributes["pac.outcome"] == "AuthorizationError"


class Stalled(SpanExporter):
    def export(self, spans):
        time.sleep(3)
        return SpanExportResult.FAILURE

    def shutdown(self):
        pass


class Exploding(SpanExporter):
    def export(self, spans):
        raise ConnectionError("collector down")

    def shutdown(self):
        pass


@pytest.mark.parametrize("processor", [
    lambda: BatchSpanProcessor(RedactingSpanExporter(Stalled()), schedule_delay_millis=10),
    lambda: SimpleSpanProcessor(RedactingSpanExporter(Exploding())),
], ids=["stalled-collector", "collector-raises"])
def test_answers_do_not_wait_for_or_fail_with_the_collector(pipeline, exec_user, processor):
    question = "total paid pack units last quarter"
    pipeline.ask(exec_user, question)                       # warm caches
    started = time.perf_counter()
    baseline = pipeline.ask(exec_user, question)
    untraced = time.perf_counter() - started

    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor())
    telemetry.use(provider, None)
    try:
        started = time.perf_counter()
        result = pipeline.ask(exec_user, question)
        traced_s = time.perf_counter() - started
    finally:
        telemetry.reset()
    assert baseline.status == result.status == "answered"
    assert traced_s < untraced + 1.0, (traced_s, untraced)
