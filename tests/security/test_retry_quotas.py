"""Per-user limits apply to every attempt that does work, retries included.

Review of 1 October 2026, R2. Only a COMMITTED outcome is replayed; a run
that failed, lost a revision race, was abandoned by a dead worker or was
cancelled is run again under the same key. That reclaim returned before the
per-user limits were checked, and the rate windows counted runs by their
original creation time, so a retry was never counted. A client could retry a
failing request without limit, across conversations and workers.

The first nine reproduced it on the unmodified code
(evidence/runs/r3-r2-reproduced.json). These call the real acquisition path
(runs.acquire) against PostgreSQL, the way each worker does. Two workers are two threads with their own database
sessions: the limits are serialised in the database, not in a process.
"""

from __future__ import annotations

import secrets
import threading

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security

PLENTY = 1000


def principal(identity):
    from app.auth.policy import principal_for_user_id
    return principal_for_user_id(identity.user_id)


def conversation(p) -> str:
    from app.conversation.state import open_conversation
    return open_conversation(p, None).conversation_id


def key() -> str:
    return "k-" + secrets.token_hex(6)


def acquire(p, conv, k, limits):
    from app.conversation import runs
    return runs.acquire(p, conv, revision=0, request_hash="h-" + k, idempotency_key=k,
                        limits=limits)


def failed(p, limits, *, status="failed"):
    """A request that ran and did not commit: (conversation, key)."""
    from app.conversation import runs
    conv, k = conversation(p), key()
    runs.fail(acquire(p, conv, k, limits), None, status=status)
    return conv, k


def backdate(p, k, interval):
    """As if this key's runs, and their recorded attempts, started `interval` ago."""
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("UPDATE app_conv.runs SET created_at = created_at - %s::interval "
                    "WHERE owner_user_id = %s AND idempotency_key = %s",
                    (interval, p.user_id, k))
        cur.execute("SELECT to_regclass('app_conv.run_attempts') IS NOT NULL AS present")
        if cur.fetchone()["present"]:
            cur.execute("UPDATE app_conv.run_attempts a SET started_at = started_at - %s::interval "
                        "FROM app_conv.runs r WHERE a.run_id = r.run_id "
                        "AND r.owner_user_id = %s AND r.idempotency_key = %s",
                        (interval, p.user_id, k))


@pytest.fixture
def quota():
    from app.conversation import runs
    return runs.QuotaExceeded


# -- the bypass ------------------------------------------------------------------------

def test_a_retry_after_a_failure_is_counted_against_the_rate(make_identity, quota):
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (1, PLENTY, PLENTY)
    conv, k = failed(p, limits)                      # the one attempt this minute allows
    with pytest.raises(quota):
        acquire(p, conv, k, limits)                  # the retry is a second attempt


def test_every_retry_of_a_failing_request_is_counted(make_identity, quota):
    """A provider failing repeatedly: each retry runs the model again."""
    from app.conversation import runs
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (3, PLENTY, PLENTY)
    conv, k = failed(p, limits)                      # attempt 1
    for _ in range(2):                               # attempts 2 and 3
        runs.fail(acquire(p, conv, k, limits), None)
    with pytest.raises(quota):
        acquire(p, conv, k, limits)                  # a fourth in the same minute


def test_a_retry_counts_toward_concurrent_runs(make_identity, quota):
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (PLENTY, PLENTY, 1)
    conv, k = failed(p, limits)
    acquire(p, conversation(p), key(), limits)       # another request, still running
    with pytest.raises(quota):
        acquire(p, conv, k, limits)


def test_a_retry_in_another_conversation_is_still_the_same_users_attempt(make_identity, quota):
    """Limits are per user, not per conversation: a failed request in one
    conversation retried while the user is busy in a second is refused."""
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (2, PLENTY, PLENTY)
    first_conv, first_key = failed(p, limits)        # attempt 1, conversation A
    failed(p, limits)                                # attempt 2, conversation B
    with pytest.raises(quota):
        acquire(p, first_conv, first_key, limits)    # retrying A is attempt 3


def test_a_retry_long_after_the_original_is_counted_when_it_runs(make_identity, quota):
    """The rate counts attempts by when they run, not by when the key was
    first used: a retry of an old failure is a new attempt now."""
    from app.conversation import runs
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (2, PLENTY, PLENTY)
    conv, k = failed(p, limits)
    backdate(p, k, "2 hours")                        # the original is outside every window
    runs.fail(acquire(p, conv, k, limits), None)     # retried now: attempt 1 this minute
    acquire(p, conversation(p), key(), limits)       # a new request: attempt 2
    with pytest.raises(quota):
        acquire(p, conversation(p), key(), limits)   # attempt 3


def test_a_retry_of_an_abandoned_run_is_counted(make_identity, quota):
    """A worker died mid-run; its lease expired. Reclaiming it runs again."""
    from app.db import owner_transaction
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (1, PLENTY, PLENTY)
    conv, k = conversation(p), key()
    acquire(p, conv, k, limits)
    with owner_transaction() as cur:
        cur.execute("UPDATE app_conv.runs SET lease_expires_at = now() - interval '1 second' "
                    "WHERE owner_user_id = %s AND idempotency_key = %s", (p.user_id, k))
    with pytest.raises(quota):
        acquire(p, conv, k, limits)


def test_a_retry_of_a_cancelled_run_is_counted(make_identity, quota):
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (1, PLENTY, PLENTY)
    conv, k = failed(p, limits, status="cancelled")
    with pytest.raises(quota):
        acquire(p, conv, k, limits)


def test_deleting_a_conversation_does_not_refund_its_attempts(make_identity, quota):
    """Runs are deleted with their conversation. If the rate were counted
    from runs, deleting conversations would reset it."""
    from app.db import owner_transaction
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (1, PLENTY, PLENTY)
    conv, _ = failed(p, limits)
    with owner_transaction() as cur:
        cur.execute("DELETE FROM app_conv.conversations WHERE conversation_id = %s", (conv,))
    with pytest.raises(quota):
        acquire(p, conversation(p), key(), limits)


def test_two_workers_retrying_cannot_both_take_the_last_slot(make_identity):
    """Two failed requests in two conversations, retried at the same moment
    by two workers (two database sessions) with one concurrent slot left.
    Exactly one may run."""
    from app.conversation import runs
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (PLENTY, PLENTY, 1)
    pending = [failed(p, limits), failed(p, limits)]
    barrier, outcomes = threading.Barrier(2), []

    def retry(conv, k):
        barrier.wait(5)
        try:
            acquire(p, conv, k, limits)
            outcomes.append("admitted")
        except runs.QuotaExceeded:
            outcomes.append("refused")

    threads = [threading.Thread(target=retry, args=item) for item in pending]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(outcomes) == ["admitted", "refused"]


# -- what must not change ------------------------------------------------------------

def test_replaying_a_committed_answer_is_free(client, make_identity, monkeypatch):
    """Returning a stored outcome costs nothing worth limiting: the same key
    replays after the user's allowance is spent, and is never re-run."""
    import app.api.main as api
    settings = api.pipeline().settings
    monkeypatch.setattr(settings, "user_requests_per_minute", 1)
    sign_in(client, make_identity("exec", can_view_wac=1))
    body = {"question": "What is our total volume this quarter?"}
    same = {"Idempotency-Key": key()}
    first = client.post("/api/ask", json=body, headers=same)
    assert first.status_code == 200 and first.json()["status"] == "answered"
    for _ in range(3):
        again = client.post("/api/ask", json=body, headers=same)
        assert again.status_code == 200 and again.json().get("replayed") is True
        assert again.json()["run_id"] == first.json()["run_id"]
    other = client.post("/api/ask", json=body, headers={"Idempotency-Key": key()})
    assert other.status_code == 429


def test_a_finished_or_cancelled_run_frees_its_concurrent_slot(make_identity):
    from app.conversation import runs
    p = principal(make_identity("exec", can_view_wac=1))
    limits = (PLENTY, PLENTY, 1)
    run = acquire(p, conversation(p), key(), limits)
    runs.fail(run, None, status="cancelled")
    acquire(p, conversation(p), key(), limits)
