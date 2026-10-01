"""Ingestion is observable from the process that runs it, and a stopped feed
is visible without waiting for a batch that will never come.

Review of 1 October 2026, R4. Telemetry was configured by the API's
lifespan; ingestion runs in the separate one-shot jobs process, whose entry
point (scripts/ingest.py) never configured or shut it down -- so its
instruments were no-ops even with a collector set. And the stale-feed alert
watched a lag set only when a batch landed: a healthy last batch followed
by silence left it looking healthy for ever.

The collector here is a local OTLP/HTTP receiver in a thread, decoding what
the real exporter sends. No external service is contacted.
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

R4 = pytest.mark.xfail(strict=True, reason="R4: ingestion telemetry is not configured in "
                                           "the jobs process, and freshness does not age")


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
    env = {**os.environ, "PAC_DB_NAME": INGEST_DB, "PYTHONPATH": str(ROOT),
           "PAC_OTEL_TIMEOUT_S": "2"}
    env.pop("PAC_OTEL_ENDPOINT", None)
    if endpoint:
        env["PAC_OTEL_ENDPOINT"] = endpoint
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ingest.py"), *args],
                          env=env, capture_output=True, text=True, timeout=timeout)
    return proc, time.monotonic() - started


# -- the jobs process exports what it measures ---------------------------------------

@R4
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
def exported():
    """This process's telemetry, read back in memory."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from app import telemetry

    reader = InMemoryMetricReader()
    telemetry.use(None, MeterProvider(metric_readers=[reader]))
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


@R4
def test_freshness_keeps_ageing_after_the_feed_stops(fresh, exported):
    """A healthy batch, then silence. Some exported signal must grow by the
    silence without another batch arriving -- the review's stopped feed."""
    assert run(batch("fresh-1", ev("fresh-1"))).status == "published"
    before = gauges_for(exported, SOURCE)
    backdate(SOURCE, "3 days")
    after = gauges_for(exported, SOURCE)
    aged = {name: after[name] - before.get(name, 0.0) for name in after}
    assert any(delta >= 3 * 86_400 - 120 for delta in aged.values()), (before, after)
