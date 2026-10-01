"""The turn graph on PostgreSQL: paused, restarted, resumed -- by whom, with what.

Review Phase 3 acceptance: "permissions changed during a pause prevent
unauthorized resume/readback", and a graph that wraps the existing
components without becoming a second source of truth.

* A clarification is a paused thread in app_graph; the domain row in
  app_conv names it. A reply resumes THAT thread, after a restart, and the
  thread is pruned once the turn commits.
* What a checkpoint holds: the question, the choices shown, the plan,
  versions. Not the principal, not an answer, not revenue.
* Nobody resumes a thread except through their own conversation.
* A run that dies after planning resumes without planning again.
* A checkpoint from another graph version is restarted, not resumed.
* The request deadline stops a run between steps.
* The serving role can record workflow state and cannot alter its schema.
"""

from __future__ import annotations

import secrets

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security

TWIN = "Pactest Graph Twin"


@pytest.fixture
def twins(authtest_db):
    from app.analytics.mentions import clear_caches
    from app.db import owner_transaction

    suffix = secrets.token_hex(3).upper()
    rows = [(f"SA-GT1-{suffix}", "TX", "75201"), (f"SA-GT2-{suffix}", "OR", "97201")]
    with owner_transaction() as cur:
        for org_id, state, zip_code in rows:
            cur.execute(
                "INSERT INTO organizations (org_id, org_name, org_type, org_status, state, zip) "
                "VALUES (%s, %s, 'Facility', 'Active', %s, %s)", (org_id, TWIN, state, zip_code))
    clear_caches()
    yield [r[0] for r in rows]
    with owner_transaction() as cur:
        cur.execute("DELETE FROM organizations WHERE org_id = ANY(%s)", ([r[0] for r in rows],))
    clear_caches()


def ask(client, question, conversation_id=None, key=None):
    body = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return client.post("/api/ask", json=body,
                       headers={"Idempotency-Key": key} if key else {})


def pending_thread(conversation_id):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT graph_thread_id FROM app_conv.clarifications "
                    "WHERE conversation_id = %s AND status = 'pending'", (conversation_id,))
        row = cur.fetchone()
    return row["graph_thread_id"] if row else None


def checkpoint_rows(thread_id):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM app_graph.checkpoints WHERE thread_id = %s",
                    (thread_id,))
        return cur.fetchone()["n"]


def checkpoint_text(thread_id):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT checkpoint::text || metadata::text AS t "
                    "FROM app_graph.checkpoints WHERE thread_id = %s", (thread_id,))
        parts = [r["t"] for r in cur.fetchall()]
        cur.execute("SELECT encode(blob, 'escape') AS b FROM app_graph.checkpoint_blobs "
                    "WHERE thread_id = %s AND blob IS NOT NULL", (thread_id,))
        parts += [r["b"] for r in cur.fetchall()]
        cur.execute("SELECT encode(blob, 'escape') AS b FROM app_graph.checkpoint_writes "
                    "WHERE thread_id = %s", (thread_id,))
        parts += [r["b"] for r in cur.fetchall()]
    return "\n".join(parts)


def restart():
    import app.api.main as api
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools
    from app.llm.planner import build_planner
    from app.pipeline import Pipeline

    close_pools()
    clear_caches()
    api._pipeline = Pipeline(build_planner(get_settings()))


# ---------------------------------------------------------------------------
# Pause, restart, resume, prune
# ---------------------------------------------------------------------------

def test_a_clarification_is_a_paused_thread_that_a_reply_resumes(client, make_identity, twins):
    sign_in(client, make_identity("exec", can_view_wac=1))
    first = ask(client, f"What was the revenue for {TWIN} in the last 3 months?").json()
    assert first["status"] == "clarify"
    thread = pending_thread(first["conversation_id"])
    assert thread and checkpoint_rows(thread) > 0, "no paused thread was checkpointed"

    restart()
    second = ask(client, "the second one", first["conversation_id"]).json()

    assert second["status"] == "answered", second
    assert checkpoint_rows(thread) == 0, "a finished thread was kept"


def test_a_paused_checkpoint_holds_no_identity_and_no_answer(client, make_identity, twins):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = ask(client, f"What was the revenue for {TWIN} in the last 3 months?").json()
    text = checkpoint_text(pending_thread(first["conversation_id"]))

    assert TWIN in text, "the test is not looking at the paused thread"
    for secret in (user.user_id, user.email, user.password, "$", "wac_authorized",
                   "scope_value", "session"):
        assert secret not in text, f"{secret!r} was written to a checkpoint"


def test_a_completed_answer_leaves_no_checkpoint(client, make_identity):
    """An answer's thread is pruned as soon as it commits: revenue in a
    headline never sits in workflow storage."""
    from app.db import owner_transaction
    sign_in(client, make_identity("exec", can_view_wac=1))
    body = ask(client, "What is our total revenue this quarter?").json()
    assert "$" in body["answer"]["headline"]
    with owner_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM app_graph.checkpoints WHERE thread_id LIKE %s",
                    (body["conversation_id"] + ".%",))
        assert cur.fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Who may resume
# ---------------------------------------------------------------------------

def test_another_user_cannot_resume_a_paused_thread(client, make_identity, twins):
    alice, bob = make_identity("exec", can_view_wac=1), make_identity("exec", can_view_wac=1)
    sign_in(client, alice)
    first = ask(client, f"What was the volume for {TWIN} in the last 3 months?").json()
    thread = pending_thread(first["conversation_id"])

    sign_in(client, bob)
    r = ask(client, "the second one", first["conversation_id"])
    assert r.status_code == 404                     # not "exists but not yours"
    # Nor can a thread be named directly: there is no field for it, and an
    # unknown field is ignored rather than honoured.
    r = client.post("/api/ask", json={"question": "the second one", "thread_id": thread,
                                      "include_sql": True})
    assert r.status_code == 200
    assert not any(t in str(r.json().get("plan")) for t in twins), "Bob reached Alice's choice"
    assert checkpoint_rows(thread) > 0, "Bob's requests touched Alice's thread"


def test_a_scope_change_during_the_pause_prevents_the_resume(client, make_identity, twins):
    """The pause outlived the access it was asked under. The reply cannot
    resume it; the conversation starts fresh under the new scope."""
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = ask(client, f"What was the volume for {TWIN} in the last 3 months?").json()
    user.change(can_view_wac=0)

    second = ask(client, "the second one", first["conversation_id"]).json()
    assert second["status"] != "answered" or second["conversation_id"] != first["conversation_id"]
    assert not any(t in str(second.get("plan")) for t in twins)


# ---------------------------------------------------------------------------
# Crash, version change, deadline
# ---------------------------------------------------------------------------

def test_a_run_that_dies_after_planning_resumes_without_planning_again(
        client, make_identity, monkeypatch):
    import app.api.main as api
    from app.pipeline import Turn

    planned = []
    original_plan = api.pipeline().planner.plan
    monkeypatch.setattr(api.pipeline().planner, "plan",
                        lambda q, c: planned.append(q) or original_plan(q, c))
    crashed = {"done": False}
    original_answer = Turn.node_answer

    def crash_once(self, state):
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("worker died after planning")
        return original_answer(self, state)

    monkeypatch.setattr(Turn, "node_answer", crash_once)
    sign_in(client, make_identity("exec", can_view_wac=1))
    key = "k-" + secrets.token_hex(8)

    crashed_response = ask(client, "What is our total volume this quarter?", key=key)
    assert crashed_response.status_code == 500      # the API's catch-all, with an id
    retried = ask(client, "What is our total volume this quarter?", key=key).json()

    assert retried["status"] == "answered"
    assert len(planned) == 1, "the retry planned again instead of resuming"


def test_a_checkpoint_from_another_graph_version_is_restarted(client, make_identity, monkeypatch):
    """The first state this request writes is stamped with an older graph
    version -- as a thread checkpointed before a deploy would be. The node
    guard refuses to resume it, the thread is discarded, and the question is
    answered on a fresh thread under the current version."""
    import app.pipeline as pipeline_module

    sign_in(client, make_identity("exec", can_view_wac=1))
    original = pipeline_module.Turn.initial_state
    stamped = {"older": 0}

    def older_once(self, **kw):
        state = original(self, **kw)
        if not stamped["older"]:
            stamped["older"] += 1
            state["graph_version"] = "0.0.0-older"
        return state

    monkeypatch.setattr(pipeline_module.Turn, "initial_state", older_once)
    r = ask(client, "What is our total volume this quarter?")

    assert stamped["older"] == 1, "the stale state was never written"
    assert r.status_code == 200 and r.json()["status"] == "answered", r.text


def test_the_request_deadline_stops_a_run_between_steps(client, make_identity, monkeypatch):
    import app.api.main as api
    monkeypatch.setattr(api.pipeline().settings, "request_deadline_seconds", -1)
    sign_in(client, make_identity("exec", can_view_wac=1))

    body = ask(client, "What is our total volume this quarter?").json()
    assert body["status"] == "error"
    assert "too long" in body["message"]
    assert body["persistence"] == "not_saved"


# ---------------------------------------------------------------------------
# The runtime role and the graph store
# ---------------------------------------------------------------------------

def test_the_serving_role_writes_workflow_state_and_cannot_alter_it(authtest_db):
    import psycopg

    from app.config import get_settings
    from app.db import graph_pool

    with graph_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM checkpoints")        # DML: allowed
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("CREATE TABLE app_graph.intruder (x int)")
    with graph_pool().connection() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DROP TABLE app_graph.checkpoints")
    assert get_settings().db_auth_user != get_settings().db_owner_user


def test_external_tracing_is_off_unless_explicitly_enabled(monkeypatch):
    import os

    from app.graph import disable_external_tracing

    monkeypatch.delenv("PAC_LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    disable_external_tracing()
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_pruning_removes_threads_that_can_no_longer_resume(client, make_identity, twins):
    from app.db import owner_transaction
    from app.graph import checkpointer
    from app.graph.retention import prune

    sign_in(client, make_identity("exec", can_view_wac=1))
    paused = ask(client, f"What was the volume for {TWIN} in the last 3 months?").json()
    kept = pending_thread(paused["conversation_id"])

    stale = ask(client, f"What was the volume for {TWIN} last quarter?").json()
    gone = pending_thread(stale["conversation_id"])
    with owner_transaction() as cur:
        cur.execute("UPDATE app_conv.clarifications SET expires_at = now() - interval '1 minute' "
                    "WHERE graph_thread_id = %s", (gone,))

    result = prune(checkpointer(), orphan_hours=24)

    assert result.clarifications_expired >= 1
    assert checkpoint_rows(gone) == 0, "an expired clarification's thread was kept"
    assert checkpoint_rows(kept) > 0, "a still-pending clarification's thread was pruned"
