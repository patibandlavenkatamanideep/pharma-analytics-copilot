"""Per-user limits that hold across replicas, and cancellation.

Review Phase 4: "per-user quotas and bounded concurrency. Controls that must
work across replicas cannot depend solely on process-local counters." The
limits are counted from app_conv.runs in the database and serialised per
user, so every worker and replica shares one allowance.

Cancellation is cooperative: the owner flags a running request, and the
graph stops at its next step boundary with nothing recorded.
"""

from __future__ import annotations

import secrets

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security

Q = {"question": "What is our total volume this quarter?"}


@pytest.fixture
def limits(monkeypatch):
    import app.api.main as api

    def set_limits(per_minute=1000, per_hour=1000, concurrent=1000):
        s = api.pipeline().settings
        monkeypatch.setattr(s, "user_requests_per_minute", per_minute)
        monkeypatch.setattr(s, "user_requests_per_hour", per_hour)
        monkeypatch.setattr(s, "user_concurrent_runs", concurrent)
    return set_limits


def test_the_per_minute_limit_refuses_with_429_and_retry_after(client, make_identity, limits):
    limits(per_minute=3)
    sign_in(client, make_identity("exec", can_view_wac=1))
    codes = [client.post("/api/ask", json=Q).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    r = client.post("/api/ask", json=Q)
    assert r.json()["detail"]["code"] == "rate_limited"
    assert r.headers["Retry-After"] == "60"


def test_the_limit_is_per_user(client, make_identity, limits):
    limits(per_minute=1)
    sign_in(client, make_identity("exec", can_view_wac=1))
    assert client.post("/api/ask", json=Q).status_code == 200
    sign_in(client, make_identity("exec", can_view_wac=1))
    assert client.post("/api/ask", json=Q).status_code == 200


def test_the_limit_is_counted_in_the_database_not_in_a_worker(client, make_identity, limits):
    """A second process sharing the database sees the same count. Simulated
    by recording runs directly, as another worker would."""
    from app.db import owner_transaction

    limits(per_minute=2)
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = client.post("/api/ask", json=Q).json()
    with owner_transaction() as cur:
        cur.execute(
            "INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, payload_hash, "
            "  scope_fingerprint, base_revision, status, lease_expires_at, expires_at) "
            "VALUES (%s, %s, %s, 'other-worker', 'x', 0, 'succeeded', now(), "
            "        now() + interval '1 day')",
            ("r_other_" + secrets.token_hex(4), first["conversation_id"], user.user_id))
    assert client.post("/api/ask", json=Q).status_code == 429


def test_concurrent_runs_are_bounded(client, make_identity, limits):
    from app.db import owner_transaction

    limits(concurrent=1)
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = client.post("/api/ask", json=Q).json()
    with owner_transaction() as cur:          # a request still running elsewhere
        cur.execute(
            "INSERT INTO app_conv.conversations (conversation_id, owner_user_id, "
            "  scope_fingerprint) SELECT 'c_live_' || %s, owner_user_id, scope_fingerprint "
            "FROM app_conv.conversations WHERE conversation_id = %s",
            (secrets.token_hex(3), first["conversation_id"]))
        cur.execute(
            "INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, payload_hash, "
            "  scope_fingerprint, base_revision, status, lease_expires_at, expires_at) "
            "SELECT %s, conversation_id, owner_user_id, 'x', 'x', 0, 'running', "
            "  now() + interval '5 minutes', now() + interval '1 day' "
            "FROM app_conv.conversations WHERE owner_user_id = %s AND conversation_id LIKE 'c_live_%%'",
            ("r_live_" + secrets.token_hex(4), user.user_id))
    r = client.post("/api/ask", json=Q)
    assert r.status_code == 429
    assert "being answered" in r.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_a_cancelled_request_stops_at_the_next_step_and_records_nothing(
        client, make_identity, monkeypatch):
    from app.conversation import runs
    from app.db import owner_transaction
    from app.pipeline import Turn

    original = Turn.node_plan

    def plan_then_cancelled(self, state):
        out = original(self, state)
        # As if the owner pressed Stop while the model was planning.
        runs.request_cancel(self.principal, run_id=self.run.run_id)
        return out

    monkeypatch.setattr(Turn, "node_plan", plan_then_cancelled)
    sign_in(client, make_identity("exec", can_view_wac=1))
    body = client.post("/api/ask", json=Q).json()

    assert body["status"] == "cancelled"
    assert body["persistence"] == "not_saved"
    with owner_transaction() as cur:
        cur.execute("SELECT status FROM app_conv.runs WHERE run_id = %s", (body["run_id"],))
        assert cur.fetchone()["status"] == "cancelled"
        cur.execute("SELECT count(*) AS n FROM app_conv.turns WHERE conversation_id = %s",
                    (body["conversation_id"],))
        assert cur.fetchone()["n"] == 0


def test_a_request_can_be_cancelled_by_its_key_before_its_run_id_is_known(
        client, make_identity):
    from app.db import owner_transaction

    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = client.post("/api/ask", json=Q).json()
    key = "k-" + secrets.token_hex(8)
    with owner_transaction() as cur:
        cur.execute(
            "INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, idempotency_key, "
            "  payload_hash, scope_fingerprint, base_revision, status, lease_expires_at, expires_at) "
            "VALUES (%s, %s, %s, %s, 'x', 'x', 0, 'running', now() + interval '5 minutes', "
            "        now() + interval '1 day')",
            ("r_k_" + secrets.token_hex(4), first["conversation_id"], user.user_id, key))
    r = client.post("/api/runs/cancel", json={"idempotency_key": key})
    assert r.status_code == 200 and r.json()["status"] == "cancel_requested"


def test_nobody_else_can_cancel_your_request(client, make_identity):
    from app.db import owner_transaction

    alice, bob = make_identity("exec", can_view_wac=1), make_identity("exec", can_view_wac=1)
    sign_in(client, alice)
    first = client.post("/api/ask", json=Q).json()
    run_id = "r_a_" + secrets.token_hex(4)
    with owner_transaction() as cur:
        cur.execute(
            "INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, payload_hash, "
            "  scope_fingerprint, base_revision, status, lease_expires_at, expires_at) "
            "VALUES (%s, %s, %s, 'x', 'x', 0, 'running', now() + interval '5 minutes', "
            "        now() + interval '1 day')",
            (run_id, first["conversation_id"], alice.user_id))
    sign_in(client, bob)
    assert client.post("/api/runs/cancel", json={"run_id": run_id}).status_code == 404
    with owner_transaction() as cur:
        cur.execute("SELECT cancel_requested_at FROM app_conv.runs WHERE run_id = %s", (run_id,))
        assert cur.fetchone()["cancel_requested_at"] is None
