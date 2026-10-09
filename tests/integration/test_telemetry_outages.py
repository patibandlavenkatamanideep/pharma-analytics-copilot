"""Telemetry during an outage: the freshness read and shutdown stay bounded
when the database, its pool or the collector is down, or all of them are.

Review of 2 October 2026, finding 6. The freshness read waited on the
serving auth pool -- up to its 30 s timeout -- before it set its 2 s
statement timeout, so an unreachable database, or a pool busy with
requests, held the metrics thread for half a minute per read. Shutdown took
its bound from the global settings rather than the configure() timeout.

Written on the parallel branch post-assessment/release-risks and ported
here. On the reviewed code, 58b3d3e, the five outage cases fail; the
locked-table and silent-collector-only cases pass there too, and stay as
guards (evidence/runs/r5-port-reproduced.json). Real connections throughout: a
closed port, a socket that accepts and never answers, the real pool held
by the test, and an HTTP collector that never responds.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from types import SimpleNamespace

def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def black_hole():
    """A port that accepts connections and never answers: the kernel
    completes the handshake, nothing reads or replies."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


@contextlib.contextmanager
def database_at(host: str, port: int):
    """The application's settings and pools pointed at another address."""
    from app.config import get_settings
    from app.db import close_pools

    saved = {k: os.environ.get(k) for k in ("PAC_DB_HOST", "PAC_DB_PORT")}
    os.environ.update(PAC_DB_HOST=host, PAC_DB_PORT=str(port))
    get_settings.cache_clear()
    close_pools()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        close_pools()


def timed(fn):
    started = time.perf_counter()
    try:
        return fn(), None, time.perf_counter() - started
    except Exception as exc:
        return None, exc, time.perf_counter() - started


def test_the_freshness_read_is_bounded_when_the_database_is_unreachable():
    from app.data import freshness
    with database_at("127.0.0.1", closed_port()):
        found, error, elapsed = timed(freshness.read)
    assert error is not None, found
    assert elapsed < 5, f"the read took {elapsed:.1f} s with nothing listening"


def test_the_freshness_read_is_bounded_when_the_database_never_answers():
    from app.data import freshness
    with black_hole() as port, database_at("127.0.0.1", port):
        found, error, elapsed = timed(freshness.read)
    assert error is not None, found
    assert elapsed < 6, f"the read took {elapsed:.1f} s against a silent server"


def test_the_freshness_read_does_not_wait_behind_requests_for_the_serving_pool():
    """Every auth-pool connection is in use, as under load. The read neither
    waits for one nor takes one from a request: it still answers, promptly."""
    from app.data import freshness
    from app.db import close_pools, get_pool

    close_pools()
    pool = get_pool("auth")
    held = [pool.getconn(timeout=10) for _ in range(pool.max_size)]
    try:
        found, error, elapsed = timed(freshness.read)
    finally:
        for conn in held:
            pool.putconn(conn)
    assert error is None, repr(error)
    assert isinstance(found, list)
    assert elapsed < 3, f"the read took {elapsed:.1f} s with the pool busy"


def test_shutdown_is_bounded_with_the_collector_and_the_database_both_down():
    """A collector that accepts and never answers, no database, and a 1 s
    exporter timeout given to configure(): spans and metrics are recorded,
    and shutdown returns within 2 x 1 + 1 = 3 s."""
    from app import telemetry

    with black_hole() as collector, database_at("127.0.0.1", closed_port()):
        telemetry.reset()
        telemetry.configure(SimpleNamespace(otel_endpoint=f"http://127.0.0.1:{collector}",
                                            otel_timeout_s=1, release="test"))
        for _ in range(20):
            with telemetry.span("pac.ask", **{"pac.status": "answered"}):
                telemetry.count("pac.ask.outcomes", status="answered", role="exec")
        _, error, elapsed = timed(telemetry.shutdown)
    assert error is None, repr(error)
    assert elapsed < 3.5, f"shutdown took {elapsed:.1f} s; configure() bounded it at 3 s"


# -- regressions around the fix ---------------------------------------------------------

def test_a_statement_held_by_a_lock_is_cancelled_within_the_bound():
    """Connected, but the table is locked by another transaction: the
    statement timeout -- set on the read's own connection -- still applies."""
    import psycopg

    from app.data import freshness
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute("LOCK TABLE app_ingest.watermarks IN ACCESS EXCLUSIVE MODE")
        found, error, elapsed = timed(freshness.read)
    assert isinstance(error, psycopg.errors.QueryCanceled), repr(error or found)
    assert elapsed < 4, f"the read took {elapsed:.1f} s behind the lock"


def test_a_collection_during_a_database_outage_keeps_the_other_metrics():
    """The real read against a server that never answers: the collection
    returns within the deadline with every other metric in it, and the next
    one, inside the backoff, does not wait at all."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from app import telemetry
    from app.data import freshness

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    with black_hole() as port, database_at("127.0.0.1", port):
        telemetry.use(None, provider, freshness=freshness.read)
        try:
            telemetry.count("pac.ask.outcomes", status="answered", role="exec")
            data, error, first = timed(reader.get_metrics_data)
            _, _, second = timed(reader.get_metrics_data)
        finally:
            telemetry.reset()
            provider.shutdown(timeout_millis=1000)
    assert error is None, repr(error)
    names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert "pac.ask.outcomes" in names and "pac.ingest.since_success" not in names
    assert first < 3, f"the collection waited {first:.1f} s on the read"
    assert second < 0.5, f"inside the backoff the collection still waited {second:.1f} s"


def test_shutdown_with_a_silent_collector_is_bounded_by_its_configured_timeout():
    """The database is up; the collector accepts and never answers."""
    from app import telemetry

    with black_hole() as collector:
        telemetry.reset()
        telemetry.configure(SimpleNamespace(otel_endpoint=f"http://127.0.0.1:{collector}",
                                            otel_timeout_s=1, release="test"))
        for _ in range(20):
            with telemetry.span("pac.ask", **{"pac.status": "answered"}):
                telemetry.count("pac.ask.outcomes", status="answered", role="exec")
        _, error, elapsed = timed(telemetry.shutdown)
    assert error is None, repr(error)
    assert elapsed < 3.5, f"shutdown took {elapsed:.1f} s; configure() bounded it at 3 s"
