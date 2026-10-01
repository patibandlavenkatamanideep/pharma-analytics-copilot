"""What a failure looks like from outside: a status a client can act on, a
message a person can read, and nothing about the inside.

Real HTTP against the disposable authorization database.
"""

from __future__ import annotations

import psycopg
import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security

#: Text that must never reach a client: driver and server internals,
#: credentials, hosts, SQL.
INTERNALS = ("psycopg", "password", "127.0.0.1", "pac_", "Traceback", "SELECT", "FROM ",
             "OperationalError", "connection to server")


def leaks(response) -> list[str]:
    return [s for s in INTERNALS if s in response.text]


@pytest.fixture
def database_down(monkeypatch):
    """Every pool refuses, with the kind of message a real outage produces."""
    import app.db as db

    def refuse(role):
        raise psycopg.OperationalError(
            'connection to server at "127.0.0.1", port 5432 failed: FATAL: password '
            'authentication failed for user "pac_auth_login"')

    def start():
        monkeypatch.setattr(db, "get_pool", refuse)
    return start


def test_an_outage_is_a_503_with_retry_after_and_no_internals(client, make_identity,
                                                              database_down):
    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    database_down()
    for method, path, body in (("GET", "/api/me", None),
                               ("POST", "/api/ask", {"question": "total volume last quarter"}),
                               ("GET", "/api/conversations", None)):
        r = client.request(method, path, json=body)
        assert r.status_code == 503, (path, r.status_code, r.text)
        assert r.headers["retry-after"] == "5"
        assert r.json()["detail"]["code"] == "database_unavailable"
        assert leaks(r) == [], (path, leaks(r))


def test_readiness_reports_an_outage_without_detail_that_helps_an_attacker(client,
                                                                           database_down):
    database_down()
    r = client.get("/ready")
    assert r.status_code == 503
    assert "password" not in r.text and "127.0.0.1" not in r.text
    assert client.get("/health").status_code == 200       # liveness is not readiness


def test_an_unexpected_failure_is_a_500_with_a_reference_and_nothing_else(
        client, make_identity, monkeypatch):
    import app.api.main as api

    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)

    def explode(*args, **kwargs):
        raise KeyError("SELECT wac FROM sales -- internal detail")

    monkeypatch.setattr(api.pipeline(), "ask", explode)
    r = client.post("/api/ask", json={"question": "total volume last quarter"})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert set(detail) == {"message", "request_id"}
    assert leaks(r) == [] and "wac" not in r.text


def test_a_failed_audit_write_still_answers_and_is_counted(client, make_identity):
    """A real failure, not a mock: the serving role loses INSERT on the
    audit table for one request."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from app import telemetry
    from app.db import owner_transaction

    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    reader = InMemoryMetricReader()
    telemetry.use(None, MeterProvider(metric_readers=[reader]))
    with owner_transaction() as cur:
        cur.execute("REVOKE INSERT ON app_meta.query_audit FROM pac_auth")
    try:
        r = client.post("/api/ask", json={"question": "total paid pack units last quarter"})
    finally:
        with owner_transaction() as cur:
            cur.execute("GRANT INSERT ON app_meta.query_audit TO pac_auth")
        telemetry.reset()
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and body["persistence"] == "saved"
    with owner_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM app_meta.query_audit WHERE request_id = %s",
                    (body["request_id"],))
        assert cur.fetchone()["n"] == 0
    failures = [p for rm in reader.get_metrics_data().resource_metrics
                for sm in rm.scope_metrics for m in sm.metrics
                if m.name == "pac.persistence.failures" for p in m.data.data_points]
    assert any(dict(p.attributes) == {"kind": "audit"} and p.value >= 1 for p in failures)
