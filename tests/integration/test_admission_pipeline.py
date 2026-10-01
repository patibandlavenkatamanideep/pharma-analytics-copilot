"""Admission on the real request path: a question waits its turn, is refused
predictably when the queue is full, can be cancelled while it waits, never
waits past its deadline -- and a refused question retried with the same key
resumes after planning instead of paying for the model call again.

The gate is shrunk to one slot and held by the test, so the queue is
exercised deterministically against PostgreSQL.
"""

from __future__ import annotations

import secrets
import threading
import time

import pytest

from app import admission
from app.llm.planner import OfflinePlanner

QUESTION = "top 5 accounts by paid pack units last quarter"


class CountingPlanner(OfflinePlanner):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def plan(self, question, context):
        self.calls += 1
        return super().plan(question, context)


@pytest.fixture
def one_slot(monkeypatch):
    """The query gate with a single slot, held by the test until released."""
    from app.config import get_settings

    def configure(queue: int, wait: float = 10.0):
        settings = get_settings()
        monkeypatch.setattr(settings, "admission_query_slots", 1)
        monkeypatch.setattr(settings, "admission_query_queue", queue)
        monkeypatch.setattr(settings, "admission_query_wait_seconds", wait)
        admission.reset()
        gate = admission.query_gate()
        release, held = threading.Event(), threading.Event()

        def holder():
            with gate.admitted(max_wait=1):
                held.set()
                release.wait(30)

        t = threading.Thread(target=holder)
        t.start()
        assert held.wait(5)
        return gate, release, t

    yield configure
    admission.reset()


def pipeline():
    from app.pipeline import Pipeline
    planner = CountingPlanner()
    return Pipeline(planner), planner


def audit(request_id):
    from app.db import auth_transaction
    with auth_transaction() as cur:
        cur.execute("SELECT status FROM app_meta.query_audit WHERE request_id = %s",
                    (request_id,))
        row = cur.fetchone()
    return row and row["status"]


def run_status(user, key):
    from app.db import auth_transaction
    with auth_transaction() as cur:
        cur.execute("SELECT status FROM app_conv.runs WHERE owner_user_id = %s "
                    "AND idempotency_key = %s", (user.user_id, key))
        return [r["status"] for r in cur.fetchall()]


def test_a_queued_question_is_answered_when_a_slot_frees(one_slot, exec_user):
    gate, release, holder = one_slot(queue=4)
    pipe, _ = pipeline()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", pipe.ask(exec_user, QUESTION)))
    t.start()
    deadline = time.monotonic() + 10
    while gate.waiting == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert gate.waiting == 1, "the question did not queue"
    release.set()
    t.join(30)
    holder.join(5)
    assert out["r"].status == "answered"


def test_overload_is_refused_recorded_and_resumed_without_planning_again(one_slot, exec_user):
    gate, release, holder = one_slot(queue=0)
    pipe, planner = pipeline()
    key = f"adm-{secrets.token_hex(6)}"
    started = time.monotonic()
    with pytest.raises(admission.Overloaded) as exc:
        pipe.ask(exec_user, QUESTION, idempotency_key=key)
    assert time.monotonic() - started < 3          # refused, not timed out
    assert exc.value.reason == "queue_full" and exc.value.retry_after > 0
    assert run_status(exec_user, key) == ["failed"]
    assert planner.calls == 1

    release.set()
    holder.join(5)
    retried = pipe.ask(exec_user, QUESTION, idempotency_key=key)
    assert retried.status == "answered"
    assert planner.calls == 1, "the retry planned again instead of resuming"
    assert run_status(exec_user, key) == ["succeeded"]


def test_the_refusal_is_in_the_audit_trail(one_slot, exec_user):
    from app.db import auth_transaction

    gate, release, holder = one_slot(queue=0)
    pipe, _ = pipeline()
    with pytest.raises(admission.Overloaded):
        pipe.ask(exec_user, QUESTION)
    release.set()
    holder.join(5)
    with auth_transaction() as cur:
        cur.execute("SELECT status, denial_reason FROM app_meta.query_audit "
                    "WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (exec_user.user_id,))
        row = cur.fetchone()
    assert row["status"] == "overloaded" and "queue_full" in row["denial_reason"]


def test_a_cancel_ends_the_wait(one_slot, exec_user):
    from app.conversation import runs

    gate, release, holder = one_slot(queue=4, wait=20)
    pipe, _ = pipeline()
    key = f"adm-{secrets.token_hex(6)}"
    out = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "r", pipe.ask(exec_user, QUESTION, idempotency_key=key)))
    t.start()
    deadline = time.monotonic() + 10
    while gate.waiting == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    started = time.monotonic()
    assert runs.request_cancel(exec_user, idempotency_key=key)
    t.join(10)
    assert out["r"].status == "cancelled"
    assert time.monotonic() - started < 2
    assert gate.waiting == 0
    release.set()
    holder.join(5)


def test_the_deadline_bounds_the_wait(one_slot, exec_user, monkeypatch):
    gate, release, holder = one_slot(queue=4, wait=30)
    pipe, _ = pipeline()
    monkeypatch.setattr(pipe.settings, "request_deadline_seconds", 2)
    started = time.monotonic()
    result = pipe.ask(exec_user, QUESTION)
    elapsed = time.monotonic() - started
    release.set()
    holder.join(5)
    assert result.status == "error" and "took too long" in result.message
    assert elapsed < 4, elapsed
    assert audit(result.request_id) == "deadline_exceeded"
