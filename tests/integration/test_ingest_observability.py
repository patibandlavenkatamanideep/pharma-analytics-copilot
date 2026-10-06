"""Ingestion is observable from the process that runs it, and a stopped feed
is visible without waiting for a batch that will never come.

Review of 1 October 2026, R4. Telemetry was configured by the API's
lifespan; ingestion runs in the separate one-shot jobs process, whose entry
point (scripts/ingest.py) never configured or shut it down -- so its
instruments were no-ops even with a collector set. And the stale-feed alert
watched a lag set only when a batch landed: a healthy last batch followed
by silence left it looking healthy for ever.

The first two tests reproduced it on the unmodified code
(evidence/runs/r3-r4-reproduced.json). The collector here is a local
OTLP/HTTP receiver in a thread, decoding what the real exporter sends. No
external service is contacted.
"""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from tests.integration.test_ingestion import (  # noqa: F401  (fixtures)
    INGEST_DB, SOURCE, batch, ev, fresh, ingest_env, run,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]

class Collector:
    """An OTLP/HTTP receiver: records every export request it is sent."""

    def __init__(self, *, respond: bool = True):
        self.received: list[tuple[str, bytes]] = []
        collector = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                                   # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                collector.received.append((self.path, body))
                if not respond:
                    time.sleep(60)                               # a collector that hangs
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def metrics(self) -> dict[str, list[dict]]:
        """Metric name -> the attributes of each data point received."""
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )
        out: dict[str, list[dict]] = {}
        for path, body in self.received:
            if path != "/v1/metrics":
                continue
            request = ExportMetricsServiceRequest.FromString(body)
            for rm in request.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        kind = metric.WhichOneof("data")
                        for point in getattr(metric, kind).data_points:
                            out.setdefault(metric.name, []).append(
                                {a.key: a.value.string_value for a in point.attributes})
        return out

    def spans(self) -> list[str]:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        names = []
        for path, body in self.received:
            if path == "/v1/traces":
                request = ExportTraceServiceRequest.FromString(body)
                names += [s.name for rs in request.resource_spans
                          for ss in rs.scope_spans for s in ss.spans]
        return names


def batch_file(tmp_path, batch_id: str) -> pathlib.Path:
    """One valid in-week sale for the seed dataset, as a JSON batch."""
    doc = {"source_system": SOURCE, "batch_id": batch_id,
           "declared_count": 1, "declared_pack_units": "10",
           "events": [{"source_event_id": f"cli-{batch_id}", "event_version": 1,
                       "kind": "upsert", "event_time": "2026-09-18T10:00:00-04:00",
                       "org_id": "FA001", "ndc": "11111-0101-01",
                       "data_source": "distributor", "pack_units": 10, "unit": "packs",
                       "wac": 1200.0}]}
    path = tmp_path / f"{batch_id}.json"
    path.write_text(json.dumps(doc))
    return path


def ingest_cli(*args: str, endpoint: str | None, timeout: float = 90):
    # The span this test looks for must be sampled whatever the environment
    # says; the command itself follows OTEL_TRACES_SAMPLER, as production does.
    env = {**os.environ, "PAC_DB_NAME": INGEST_DB, "PYTHONPATH": str(ROOT),
           "PAC_OTEL_TIMEOUT_S": "2", "PAC_OTEL_SOURCE_NAMES": SOURCE, "OTEL_TRACES_SAMPLER": "always_on"}
    env.pop("OTEL_TRACES_SAMPLER_ARG", None)
    env.pop("PAC_OTEL_ENDPOINT", None)
    if endpoint:
        env["PAC_OTEL_ENDPOINT"] = endpoint
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ingest.py"), *args],
                          env=env, capture_output=True, text=True, timeout=timeout)
    return proc, time.monotonic() - started


# -- the jobs process exports what it measures ---------------------------------------

def test_the_ingest_command_exports_its_metrics_and_span_to_the_collector(fresh, tmp_path):
    collector = Collector()
    try:
        proc, _ = ingest_cli(str(batch_file(tmp_path, "otel-1")), endpoint=collector.url)
    finally:
        collector.close()
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.splitlines()[-1])["status"] == "published"
    metrics = collector.metrics()
    assert {"status": "published", "source": SOURCE} in metrics.get("pac.ingest.batches", []), \
        sorted(metrics)
    assert "pac.ingest.duration" in metrics
    assert "pac.ingest" in collector.spans()


# -- a stopped feed is visible ---------------------------------------------------------

@pytest.fixture
def exported(monkeypatch):
    """This process's telemetry, read back in memory."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from app import telemetry

    from app.data import freshness

    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "otel_source_names", SOURCE)
    reader = InMemoryMetricReader()
    # As configure() installs it in the API and jobs processes: freshness is
    # read at every collection.
    telemetry.use(None, MeterProvider(metric_readers=[reader]), freshness=freshness.read)
    try:
        yield reader
    finally:
        telemetry.reset()


def gauges_for(reader, source: str) -> dict[str, float]:
    data = reader.get_metrics_data()
    out = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in getattr(metric.data, "data_points", []):
                    if dict(point.attributes or {}).get("source") == source \
                            and hasattr(point, "value"):
                        out[metric.name] = float(point.value)
    return out


def backdate(source: str, interval: str) -> None:
    """As if `interval` passed with no batch: every persisted time moves back."""
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("UPDATE app_ingest.watermarks SET watermark = watermark - %s::interval, "
                    "last_batch_at = last_batch_at - %s::interval WHERE source_system = %s",
                    (interval, interval, source))
        cur.execute("UPDATE app_ingest.batches SET first_received_at = first_received_at "
                    "- %s::interval, last_attempt_at = last_attempt_at - %s::interval "
                    "WHERE source_system = %s", (interval, interval, source))


def test_freshness_keeps_ageing_after_the_feed_stops(fresh, exported):
    """A healthy batch, then silence. Some exported signal must grow by the
    silence without another batch arriving -- the review's stopped feed."""
    assert run(batch("fresh-1", ev("fresh-1"))).status == "published"
    before = gauges_for(exported, SOURCE)
    backdate(SOURCE, "3 days")
    after = gauges_for(exported, SOURCE)
    aged = {name: after[name] - before.get(name, 0.0) for name in after}
    assert any(delta >= 3 * 86_400 - 120 for delta in aged.values()), (before, after)


# -- a collector that fails costs nothing but its own data ----------------------------

@pytest.mark.parametrize("collector_kind", ["hangs", "refuses"])
def test_a_failing_collector_neither_fails_nor_holds_up_ingestion(fresh, tmp_path,
                                                                   collector_kind):
    """A collector that accepts and never answers, or one that is not there:
    the batch is published, the command exits 0, and the exit is bounded by
    the flush limit (2 x the 2 s exporter timeout + 1 s here), not by the
    collector."""
    if collector_kind == "hangs":
        collector = Collector(respond=False)
        endpoint = collector.url
    else:
        collector, endpoint = None, "http://127.0.0.1:9"        # nothing listens
    try:
        proc, elapsed = ingest_cli(str(batch_file(tmp_path, f"down-{collector_kind}")),
                                   endpoint=endpoint, timeout=60)
    finally:
        if collector:
            collector.close()
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.splitlines()[-1])["status"] == "published"
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT status FROM app_ingest.batches WHERE batch_id = %s",
                    (f"down-{collector_kind}",))
        assert cur.fetchone()["status"] == "published"
    assert elapsed < 20, f"the command took {elapsed:.1f} s with a {collector_kind} collector"


# -- freshness, checked without a collector ----------------------------------------------

def check(*args):
    proc, _ = ingest_cli("--check-freshness", *args, endpoint=None)
    return proc.returncode, json.loads(proc.stdout)


def test_the_freshness_check_passes_while_the_feed_delivers(fresh):
    assert run(batch("check-1", ev("check-1"))).status == "published"
    code, report = check("--max-since-success", "26h", "--source", SOURCE)
    assert code == 0 and report["problems"] == []
    assert [s["source"] for s in report["sources"]] == [SOURCE]


def test_the_freshness_check_fails_when_the_feed_stops(fresh):
    """The stopped feed, seen without any collector: three silent days are a
    missed run against a 26-hour limit."""
    assert run(batch("check-2", ev("check-2"))).status == "published"
    backdate(SOURCE, "3 days")
    code, report = check("--max-since-success", "26h", "--source", SOURCE)
    assert code == 3
    assert [p["problem"] for p in report["problems"]] == ["missed_run"]
    assert report["problems"][0]["since_success_s"] >= 3 * 86_400 - 120


def test_old_data_is_stale_even_when_batches_keep_arriving(fresh):
    """since_success and watermark_age fail differently: batches arriving
    with nothing new keep the first healthy and the second ageing."""
    assert run(batch("check-3", ev("check-3"))).status == "published"
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("UPDATE app_ingest.watermarks SET watermark = watermark - interval '30 days' "
                    "WHERE source_system = %s", (SOURCE,))
    code, report = check("--max-since-success", "26h", "--max-watermark-age", "7d")
    assert code == 3 and [p["problem"] for p in report["problems"]] == ["stale_data"]


def test_an_expected_source_that_never_delivered_is_a_problem(fresh):
    code, report = check("--source", "a-feed-that-never-came")
    assert code == 3
    assert {"source": "a-feed-that-never-came", "problem": "never_delivered"} in report["problems"]


def test_an_unreadable_freshness_is_absent_not_an_error(fresh, monkeypatch):
    """If the database cannot be read, the gauges report nothing this time --
    absence is what the alert watches -- and collection does not fail."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from app import telemetry

    def broken():
        raise ConnectionError("password=hunter2 host=db")

    reader = InMemoryMetricReader()
    telemetry.use(None, MeterProvider(metric_readers=[reader]), freshness=broken)
    try:
        assert "pac.ingest.since_success" not in gauges_for(reader, SOURCE)
    finally:
        telemetry.reset()


def test_freshness_pool_exhaustion_does_not_borrow_serving_capacity(ingest_env):
    from app.data.freshness import read
    from app.db import freshness_pool, auth_transaction
    from psycopg_pool import PoolTimeout
    pool = freshness_pool()
    # Warm once, then deliberately hold the sole collection connection.
    read()
    with pool.connection() as _held:
        started = time.perf_counter()
        with pytest.raises(PoolTimeout):
            read()
        assert time.perf_counter() - started < 1
        with auth_transaction() as cur:
            cur.execute('SELECT 1 AS n')
            assert cur.fetchone()['n'] == 1


def test_database_outage_leaves_unrelated_metrics_collectable(ingest_env, monkeypatch):
    from app import telemetry
    from app.config import get_settings
    from app.db import close_pools
    from app.data.freshness import read
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    close_pools()
    monkeypatch.setattr(get_settings(), 'db_port', 1)
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    try:
        telemetry.use(meter_provider=provider, freshness=telemetry.BoundedFreshness(read))
        telemetry.count('pac.ask.outcomes', status='answered', role='exec')
        started = time.perf_counter()
        data = reader.get_metrics_data()
        assert time.perf_counter() - started < 1
        names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
        assert 'pac.ask.outcomes' in names
    finally:
        telemetry.reset()
        provider.shutdown()
        close_pools()
