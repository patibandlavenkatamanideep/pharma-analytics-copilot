"""The two audit contracts under failure (docs/AUDIT_DECISION.md).

best_effort (the default): the audit row is written after the turn commits,
in its own transaction. A failed write is counted and the answer is still
returned; a worker that dies between the commit and that write leaves a
released, replayable answer with no audit row. These tests pin that, so the
documentation cannot claim more.

strict (PAC_AUDIT_MODE=strict): the audit row commits in the transaction that
commits the turn and the run's outcome. No answer is released -- fresh or
replayed -- unless its audit row is committed; otherwise 503, retryable under
the same idempotency key, and nothing was committed.

Each scenario is checked through what the database holds afterwards: the
run's status and outcome, the conversation's turns, and the audit rows tied
to the run (run_id) and to the request a replay repeats (replay_of).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading
import time
import uuid

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security
ROOT = pathlib.Path(__file__).resolve().parents[2]
QUESTION = "What is our total volume this quarter?"


@pytest.fixture(params=["best_effort", "strict"])
def mode(request, monkeypatch):
    import app.api.main as api
    monkeypatch.setattr(api.pipeline().settings, "audit_mode", request.param)
    return request.param


@pytest.fixture
def strict(monkeypatch):
    import app.api.main as api
    monkeypatch.setattr(api.pipeline().settings, "audit_mode", "strict")
    return api.pipeline()


def key() -> str:
    return f"audit-{uuid.uuid4().hex}"


def owner_sql(text, params=()):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute(text, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def run_for(user_id: str, idempotency_key: str) -> dict:
    [row] = owner_sql("SELECT run_id, conversation_id, status, outcome, turn_seq "
                      "FROM app_conv.runs WHERE owner_user_id = %s AND idempotency_key = %s",
                      (user_id, idempotency_key))
    return row


def audit_for(run_id: str) -> list[dict]:
    return owner_sql("SELECT request_id, status, run_id, replay_of, audit_mode "
                     "FROM app_meta.query_audit WHERE run_id = %s ORDER BY audit_id", (run_id,))


def turns(conversation_id: str) -> int:
    return owner_sql("SELECT count(*) AS n FROM app_conv.turns WHERE conversation_id = %s",
                     (conversation_id,))[0]["n"]


def principal(user):
    from app.auth.policy import principal_for_user_id
    return principal_for_user_id(user.user_id)


def audit_failures(fn):
    """Run fn, returning its result and the audit-failure count it caused."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from app import telemetry

    reader = InMemoryMetricReader()
    telemetry.use(None, MeterProvider(metric_readers=[reader]))
    try:
        out = fn()
    finally:
        data = reader.get_metrics_data()
        telemetry.reset()
    count = sum(p.value for rm in (data.resource_metrics if data else [])
                for sm in rm.scope_metrics for m in sm.metrics
                if m.name == "pac.persistence.failures"
                for p in m.data.data_points if dict(p.attributes) == {"kind": "audit"})
    return out, count


# ---------------------------------------------------------------------------
# The database refuses the insert
# ---------------------------------------------------------------------------

def test_strict_a_refused_audit_insert_withholds_the_answer_and_commits_nothing(
        client, make_identity, strict):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    k = key()
    owner_sql("REVOKE INSERT ON app_meta.query_audit FROM pac_auth")
    try:
        response, failures = audit_failures(lambda: client.post(
            "/api/ask", json={"question": QUESTION}, headers={"Idempotency-Key": k}))
    finally:
        owner_sql("GRANT INSERT ON app_meta.query_audit TO pac_auth")
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "audit_unavailable"
    assert response.headers["Retry-After"]
    assert "volume" not in response.text.lower().replace("total volume this quarter", "")
    run = run_for(user.user_id, k)
    assert run["status"] == "failed" and run["outcome"] is None and run["turn_seq"] is None
    assert turns(run["conversation_id"]) == 0 and audit_for(run["run_id"]) == []
    assert failures >= 1

    # Storage restored: the same key runs again and commits exactly once.
    again = client.post("/api/ask", json={"question": QUESTION}, headers={"Idempotency-Key": k})
    assert again.status_code == 200 and again.json()["status"] == "answered"
    assert again.json()["run_id"] == run["run_id"]
    [row] = audit_for(run["run_id"])
    assert (row["status"], row["audit_mode"], row["request_id"]) == (
        "answered", "strict", again.json()["request_id"])
    assert turns(run["conversation_id"]) == 1


# ---------------------------------------------------------------------------
# Storage unreachable at commit
# ---------------------------------------------------------------------------

def test_a_storage_outage_at_commit(make_identity, mode, monkeypatch):
    """Both the commit and the audit write meet an unreachable database."""
    import psycopg

    import app.api.main as api
    import app.pipeline as P
    from app.pipeline import AuditUnavailable

    def down(*args, **kwargs):
        raise psycopg.OperationalError("simulated: server closed the connection")

    user = make_identity("exec", can_view_wac=1)
    k = key()
    monkeypatch.setattr(P, "finalise", down)
    monkeypatch.setattr(P.Pipeline, "_insert_audit", down)
    if mode == "strict":
        with pytest.raises(AuditUnavailable):
            api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
        result = None
    else:
        result, failures = audit_failures(
            lambda: api.pipeline().ask(principal(user), QUESTION, idempotency_key=k))
        # Best effort, as documented: the answer is released, marked not
        # saved, with no audit row; the loss is counted.
        assert result.status == "answered" and result.persistence == "failed"
        assert failures == 1
    monkeypatch.undo()
    run = run_for(user.user_id, k)
    assert run["outcome"] is None and audit_for(run["run_id"]) == []


# ---------------------------------------------------------------------------
# A worker dies around the commit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("point", ["before_commit", "inside_commit", "after_commit"])
def test_a_worker_dying_around_the_commit(make_identity, mode, point):
    import app.api.main as api

    user = make_identity("exec", can_view_wac=1)
    k = key()
    child = subprocess.run(
        [sys.executable, "-m", "tests.security.audit_child", user.user_id, k, mode, point],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=180)
    assert child.returncode == {"before_commit": 70, "inside_commit": 71,
                                "after_commit": 72}[point], child.stderr[-3000:]

    run = run_for(user.user_id, k)
    first = audit_for(run["run_id"])
    if point == "after_commit":
        assert run["status"] == "succeeded" and run["outcome"] is not None
        # strict: committed with the outcome. best effort: the documented gap --
        # a committed, replayable answer and no audit row.
        assert [r["status"] for r in first] == (["answered"] if mode == "strict" else [])
    else:
        # PostgreSQL rolled the open transaction back: nothing of this run.
        assert run["status"] == "running" and run["outcome"] is None
        assert first == [] and turns(run["conversation_id"]) == 0

    time.sleep(1.2)               # the dead worker's one-second lease lapses
    result = api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
    assert result.status == "answered" and result.run_id == run["run_id"]
    rows = audit_for(run["run_id"])
    if point == "after_commit":
        assert result.payload.get("replayed") is True
        if mode == "strict":
            assert [r["status"] for r in rows] == ["answered", "replayed"]
            assert rows[1]["replay_of"] == rows[0]["request_id"] == result.request_id
        else:
            assert rows == [], "best effort: neither the release nor its replay is audited"
    else:
        assert not result.payload.get("replayed")
        [row] = rows
        assert row["status"] == "answered" and row["request_id"] == result.request_id
    assert turns(run["conversation_id"]) == 1, "exactly one committed outcome"


# ---------------------------------------------------------------------------
# Lost response, duplicate request, expired lease, revoked access
# ---------------------------------------------------------------------------

def test_a_lost_response_is_replayed_and_strict_records_the_replay(make_identity, mode):
    import app.api.main as api

    user = make_identity("exec", can_view_wac=1)
    k = key()
    first = api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
    # ... the response never reached the client, which retries the key.
    again = api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
    assert again.payload["replayed"] is True and again.request_id == first.request_id
    rows = audit_for(first.run_id)
    if mode == "strict":
        assert [(r["status"], r["replay_of"]) for r in rows] == [
            ("answered", None), ("replayed", first.request_id)]
    else:
        assert [r["status"] for r in rows] == ["answered"]


def test_strict_a_replay_whose_audit_row_cannot_be_written_is_withheld(make_identity, strict):
    from app.pipeline import AuditUnavailable

    user = make_identity("exec", can_view_wac=1)
    k = key()
    first = strict.ask(principal(user), QUESTION, idempotency_key=k)
    owner_sql("REVOKE INSERT ON app_meta.query_audit FROM pac_auth")
    try:
        with pytest.raises(AuditUnavailable):
            strict.ask(principal(user), QUESTION, idempotency_key=k)
    finally:
        owner_sql("GRANT INSERT ON app_meta.query_audit TO pac_auth")
    assert [r["status"] for r in audit_for(first.run_id)] == ["answered"]
    replay = strict.ask(principal(user), QUESTION, idempotency_key=k)
    assert replay.payload["replayed"] is True
    assert [r["status"] for r in audit_for(first.run_id)] == ["answered", "replayed"]


def test_a_duplicate_request_while_the_first_runs_is_refused_and_releases_nothing(
        make_identity, mode, monkeypatch):
    import app.api.main as api
    from app.conversation import runs
    from app.pipeline import Turn

    user = make_identity("exec", can_view_wac=1)
    k = key()
    entered, release = threading.Event(), threading.Event()
    original = Turn.node_answer

    def held(self, state):
        entered.set()
        assert release.wait(30)
        return original(self, state)

    monkeypatch.setattr(Turn, "node_answer", held)
    out: dict = {}
    worker = threading.Thread(target=lambda: out.setdefault(
        "first", api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)))
    worker.start()
    try:
        assert entered.wait(30)
        with pytest.raises(runs.RunBusy) as busy:
            api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
        assert busy.value.same_request
        assert audit_for(run_for(user.user_id, k)["run_id"]) == []
    finally:
        release.set()
        worker.join(30)
    first = out["first"]
    assert first.status == "answered"
    assert [r["status"] for r in audit_for(first.run_id)] == ["answered"]
    assert turns(first.conversation_id) == 1


def test_an_expired_lease_lets_the_next_request_commit_and_the_first_is_withheld(
        make_identity, mode, monkeypatch):
    import app.api.main as api
    from app.pipeline import Turn

    user = make_identity("exec", can_view_wac=1)
    pipe = api.pipeline()
    opener = pipe.ask(principal(user), "What is our total volume this year?")
    conversation = opener.conversation_id

    entered, release = threading.Event(), threading.Event()
    original = Turn.node_answer

    def held(self, state):
        if self.question == QUESTION:
            entered.set()
            assert release.wait(30)
        return original(self, state)

    monkeypatch.setattr(Turn, "node_answer", held)
    monkeypatch.setattr(pipe.settings, "run_lease_seconds", 1)
    out: dict = {}
    slow = threading.Thread(target=lambda: out.setdefault("slow", pipe.ask(
        principal(user), QUESTION, conversation_id=conversation, idempotency_key=key())))
    slow.start()
    try:
        assert entered.wait(30)
        time.sleep(1.2)       # the slow request's lease lapses
        quick = pipe.ask(principal(user), "What is our total volume last quarter?",
                         conversation_id=conversation, idempotency_key=key())
        assert quick.status == "answered"
    finally:
        release.set()
        slow.join(30)
    stale = out["slow"]
    assert stale.status == "conflict" and stale.answer is None, "the stale answer is withheld"
    assert [r["status"] for r in audit_for(quick.run_id)] == ["answered"]
    assert [r["status"] for r in audit_for(stale.run_id)] == ["conflict"]
    assert turns(conversation) == 2       # the opener and the quick request


def test_access_revoked_before_a_replay_refuses_it_and_records_no_release(
        make_identity, mode):
    import app.api.main as api
    from app.conversation import runs

    user = make_identity("exec", can_view_wac=1)
    k = key()
    first = api.pipeline().ask(principal(user), "What is our total revenue this quarter?",
                               idempotency_key=k)
    assert first.status == "answered"
    owner_sql("UPDATE users SET can_view_wac = 0 WHERE user_id = %s", (user.user_id,))
    with pytest.raises(runs.ReplayUnavailable):
        api.pipeline().ask(principal(user), "What is our total revenue this quarter?",
                           idempotency_key=k)
    assert [r["status"] for r in audit_for(first.run_id)] == ["answered"]


# ---------------------------------------------------------------------------
# Cancellation either side of the commit
# ---------------------------------------------------------------------------

def test_a_cancel_before_the_answer_step_releases_nothing(make_identity, mode, monkeypatch):
    import app.api.main as api
    from app.conversation import runs
    from app.pipeline import Turn

    user = make_identity("exec", can_view_wac=1)
    k = key()
    original = Turn.node_check

    def then_cancel(self, state):
        out = original(self, state)
        runs.request_cancel(self.principal, run_id=self.run.run_id)
        return out

    monkeypatch.setattr(Turn, "node_check", then_cancel)
    result = api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
    assert result.status == "cancelled" and result.answer is None
    run = run_for(user.user_id, k)
    assert run["status"] == "cancelled" and run["outcome"] is None
    assert [r["status"] for r in audit_for(run["run_id"])] == ["cancelled"]


def test_a_cancel_that_arrives_during_the_commit_is_too_late(make_identity, mode, monkeypatch):
    """Cancellation is checked between steps; the last step executes, renders
    and commits. A cancel arriving inside it changes nothing: the answer
    commits, is released, is audited as answered and replays under its key."""
    import app.api.main as api
    import app.pipeline as P
    from app.conversation import runs

    user = make_identity("exec", can_view_wac=1)
    k = key()
    real = P.finalise

    def cancel_then_commit(principal_, state, run, *args, **kwargs):
        runs.request_cancel(principal_, run_id=run.run_id)
        return real(principal_, state, run, *args, **kwargs)

    monkeypatch.setattr(P, "finalise", cancel_then_commit)
    result = api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
    monkeypatch.undo()
    assert result.status == "answered" and result.answer is not None
    run = run_for(user.user_id, k)
    assert run["status"] == "succeeded" and run["outcome"] is not None
    assert [r["status"] for r in audit_for(run["run_id"])] == ["answered"]
    replay = api.pipeline().ask(principal(user), QUESTION, idempotency_key=k)
    assert replay.payload["replayed"] is True
