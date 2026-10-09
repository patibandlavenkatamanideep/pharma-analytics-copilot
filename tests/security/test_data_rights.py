"""A user's own data: export what they may see, delete what they own, and
let retention remove the rest on schedule.

Real HTTP with real sessions against the disposable authorization database.
"""

from __future__ import annotations

import json
import secrets
from types import SimpleNamespace

import pytest

from tests.security.helpers import ask, sign_in

pytestmark = pytest.mark.security

TWIN = "Pactest Privacy Twin"


@pytest.fixture
def twins(authtest_db):
    """Two accounts with one name, so a question about it pauses for a
    choice -- a conversation with live workflow state."""
    from app.analytics.mentions import clear_caches
    from app.db import owner_transaction

    suffix = secrets.token_hex(3).upper()
    rows = [(f"SA-PT1-{suffix}", "TX", "75201"), (f"SA-PT2-{suffix}", "OR", "97201")]
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


def q(sql, params=()):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount


def rows_for(conversation_id) -> dict[str, int]:
    out = {}
    for table in ("turns", "cohorts", "clarifications", "runs"):
        out[table] = q(f"SELECT count(*) AS n FROM app_conv.{table} WHERE conversation_id = %s",
                       (conversation_id,))[0]["n"]
    out["threads"] = q("SELECT count(*) AS n FROM app_graph.checkpoints "
                       "WHERE split_part(thread_id, '.', 1) = %s", (conversation_id,))[0]["n"]
    return out


def audit_rows(user_id) -> int:
    return q("SELECT count(*) AS n FROM app_meta.query_audit WHERE user_id = %s",
             (user_id,))[0]["n"]


# -- export --------------------------------------------------------------------------

def test_export_requires_a_session(client):
    client.cookies.clear()
    assert client.get("/api/me/data").status_code == 401


def test_export_contains_your_conversations_and_nobody_elses(client, make_identity):
    me = make_identity("exec", can_view_wac=1)
    other = make_identity("exec", can_view_wac=1)
    sign_in(client, other)
    theirs = ask(client, "top 3 accounts by paid pack units last quarter")
    sign_in(client, me)
    mine = ask(client, "top 3 accounts by paid pack units last quarter")
    assert mine["status"] == "answered"

    r = client.get("/api/me/data")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    ids = [c["conversation_id"] for c in data["conversations"]]
    assert ids == [mine["conversation_id"]]
    assert theirs["conversation_id"] not in json.dumps(data)
    (conv,) = data["conversations"]
    assert conv["turns"][0]["question"] == "top 3 accounts by paid pack units last quarter"
    assert conv["turns"][0]["answer_text"]
    assert conv["cohorts"] and len(conv["cohorts"][0]["members"]) == 3
    assert conv["runs"] and data["user"]["email"] == me.email
    assert data["audit_records"]["count"] >= 1
    assert "token_hash" not in json.dumps(data) and "ip_hash" not in json.dumps(data)


def test_export_withholds_what_was_recorded_under_access_you_no_longer_hold(
        client, make_identity, real_scopes):
    home, away = real_scopes
    ram = make_identity("ram", territory=home["territory_name"])
    sign_in(client, ram)
    question = "top 3 accounts by paid pack units last quarter in my territory"
    before = ask(client, question)
    assert before["status"] == "answered"

    ram.change(territory_name=away["territory_name"])
    data = client.get("/api/me/data").json()
    assert data["conversations"] == []
    assert data["withheld_conversations"] == 1
    assert question not in json.dumps(data)


# -- deletion -------------------------------------------------------------------------

def test_deleting_a_conversation_removes_everything_hanging_off_it(client, make_identity, twins):
    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    answered = ask(client, "top 3 accounts by paid pack units last quarter")
    paused = ask(client, f"paid pack units for {TWIN} last quarter")
    assert paused["status"] == "clarify" and paused.get("choices")
    assert rows_for(paused["conversation_id"])["threads"] > 0
    assert rows_for(answered["conversation_id"])["cohorts"] == 1
    audited = audit_rows(me.user_id)

    for conv in (answered, paused):
        r = client.delete(f"/api/conversations/{conv['conversation_id']}")
        assert r.status_code == 200 and r.json() == {"deleted": True}
        assert rows_for(conv["conversation_id"]) == {
            "turns": 0, "cohorts": 0, "clarifications": 0, "runs": 0, "threads": 0}
        assert client.get(f"/api/conversations/{conv['conversation_id']}").status_code == 404
    # The security record is not erased by a deletion request.
    assert audit_rows(me.user_id) == audited


def test_another_users_conversation_cannot_be_deleted(client, make_identity):
    owner = make_identity("exec", can_view_wac=1)
    intruder = make_identity("exec", can_view_wac=1)
    sign_in(client, owner)
    conv = ask(client, "total paid pack units last quarter")["conversation_id"]
    sign_in(client, intruder)
    r = client.delete(f"/api/conversations/{conv}")
    assert r.status_code == 404
    assert client.delete("/api/conversations/no-such-conversation").status_code == 404
    assert rows_for(conv)["turns"] == 1


def test_a_conversation_with_a_question_in_flight_is_not_deleted(client, make_identity):
    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    conv = ask(client, "total paid pack units last quarter")["conversation_id"]
    q("INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, payload_hash, "
      " scope_fingerprint, base_revision, status, lease_expires_at, expires_at) "
      "VALUES (%s, %s, %s, 'x', 'x', 0, 'running', now() + interval '1 minute', "
      " now() + interval '1 day')", (f"r_busy_{secrets.token_hex(4)}", conv, me.user_id))
    r = client.delete(f"/api/conversations/{conv}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conversation_busy"
    assert client.delete("/api/me/data").status_code == 409
    assert rows_for(conv)["turns"] == 1


def test_deleting_all_your_data_covers_every_scope_and_keeps_you_signed_in(
        client, make_identity, real_scopes):
    home, away = real_scopes
    ram = make_identity("ram", territory=home["territory_name"])
    sign_in(client, ram)
    old = ask(client, "total paid pack units last quarter")["conversation_id"]
    ram.change(territory_name=away["territory_name"])
    new = ask(client, "total paid pack units last quarter")["conversation_id"]
    audited = audit_rows(ram.user_id)

    r = client.delete("/api/me/data")
    assert r.status_code == 200
    assert r.json()["deleted"]["conversations"] == 2
    assert rows_for(old)["turns"] == rows_for(new)["turns"] == 0
    assert client.get("/api/me").status_code == 200
    assert audit_rows(ram.user_id) == audited


def test_a_cross_site_deletion_is_refused_before_any_route_runs(client, make_identity):
    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    conv = ask(client, "total paid pack units last quarter")["conversation_id"]
    r = client.delete(f"/api/conversations/{conv}", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "cross_origin"
    assert rows_for(conv)["turns"] == 1


# -- retention -------------------------------------------------------------------------

def test_retention_removes_what_has_outlived_its_period(client, make_identity, twins):
    from app import retention
    from app.api.main import pipeline

    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    stale = ask(client, f"paid pack units for {TWIN} last quarter")["conversation_id"]
    fresh = ask(client, "total paid pack units last quarter")["conversation_id"]
    busy = ask(client, "top 3 accounts by paid pack units last quarter")["conversation_id"]
    assert rows_for(stale)["threads"] > 0
    q("UPDATE app_conv.conversations SET updated_at = now() - interval '200 days' "
      "WHERE conversation_id = ANY(%s)", ([stale, busy],))
    q("INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, payload_hash, "
      " scope_fingerprint, base_revision, status, lease_expires_at, expires_at) "
      "VALUES (%s, %s, %s, 'x', 'x', 0, 'running', now() + interval '1 minute', "
      " now() + interval '1 day')", (f"r_busy_{secrets.token_hex(4)}", busy, me.user_id))
    request_id = f"pactest-old-{secrets.token_hex(4)}"
    q("INSERT INTO app_meta.query_audit (request_id, user_id, role, scope_kind, "
      " wac_authorized, status, created_at) "
      "VALUES (%s, %s, 'exec', 'global', TRUE, 'answered', now() - interval '500 days')",
      (request_id, me.user_id))

    periods = SimpleNamespace(conversation_retention_days=180, audit_retention_days=400,
                              session_record_retention_days=30,
                              login_attempt_retention_days=30, quarantine_retention_days=90)
    kept = retention.apply(periods, checkpointer=pipeline().graph.checkpointer)

    assert rows_for(stale) == {"turns": 0, "cohorts": 0, "clarifications": 0, "runs": 0,
                               "threads": 0}
    assert rows_for(fresh)["turns"] == 1
    assert rows_for(busy)["turns"] == 1, "a conversation with a live request was deleted"
    assert q("SELECT count(*) AS n FROM app_meta.query_audit WHERE request_id = %s",
             (request_id,))[0]["n"] == 0
    assert audit_rows(me.user_id) >= 3          # recent audit rows are kept
    assert kept.deleted["conversations"] >= 1
    q("UPDATE app_conv.runs SET status = 'failed' WHERE conversation_id = %s", (busy,))
