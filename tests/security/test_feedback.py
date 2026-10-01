"""Feedback on answers: only the asker, only under current access, deleted
with the conversation; and the triage sample carries nothing that identifies
or quotes the user.
"""

from __future__ import annotations

import json

import pytest

from tests.security.helpers import ask, sign_in

pytestmark = pytest.mark.security

QUESTION = "top 3 accounts by paid pack units last quarter"


def feedback_rows(conversation_id):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT rating, reason, comment FROM app_conv.feedback "
                    "WHERE conversation_id = %s", (conversation_id,))
        return [dict(r) for r in cur.fetchall()]


def test_the_asker_can_rate_an_answer_and_change_their_mind(client, make_identity):
    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    answer = ask(client, QUESTION)
    r = client.post("/api/feedback", json={"run_id": answer["run_id"], "helpful": True})
    assert r.status_code == 200 and r.json() == {"recorded": True}
    r = client.post("/api/feedback", json={"run_id": answer["run_id"], "helpful": False,
                                           "reason": "wrong_number", "comment": "too low"})
    assert r.status_code == 200
    assert feedback_rows(answer["conversation_id"]) == [
        {"rating": -1, "reason": "wrong_number", "comment": "too low"}]


def test_nobody_else_can_rate_your_answer(client, make_identity):
    owner = make_identity("exec", can_view_wac=1)
    other = make_identity("exec", can_view_wac=1)
    sign_in(client, owner)
    answer = ask(client, QUESTION)
    sign_in(client, other)
    r = client.post("/api/feedback", json={"run_id": answer["run_id"], "helpful": False,
                                           "reason": "other"})
    assert r.status_code == 404
    assert client.post("/api/feedback", json={"run_id": "r_nothing", "helpful": True}
                       ).status_code == 404
    assert feedback_rows(answer["conversation_id"]) == []


def test_an_answer_recorded_under_withdrawn_access_cannot_be_rated(
        client, make_identity, real_scopes):
    home, away = real_scopes
    ram = make_identity("ram", territory=home["territory_name"])
    sign_in(client, ram)
    answer = ask(client, QUESTION)
    ram.change(territory_name=away["territory_name"])
    r = client.post("/api/feedback", json={"run_id": answer["run_id"], "helpful": True})
    assert r.status_code == 404


def test_an_unknown_reason_is_refused(client, make_identity):
    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    answer = ask(client, QUESTION)
    r = client.post("/api/feedback", json={"run_id": answer["run_id"], "helpful": False,
                                           "reason": "DROP TABLE"})
    assert r.status_code == 422


def test_feedback_is_exported_and_deleted_with_its_conversation(client, make_identity):
    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    answer = ask(client, QUESTION)
    client.post("/api/feedback", json={"run_id": answer["run_id"], "helpful": False,
                                       "reason": "missing_data", "comment": "where is March"})
    exported = client.get("/api/me/data").json()
    (conv,) = [c for c in exported["conversations"]
               if c["conversation_id"] == answer["conversation_id"]]
    assert conv["feedback"][0]["comment"] == "where is March"
    assert client.delete(f"/api/conversations/{answer['conversation_id']}").status_code == 200
    assert feedback_rows(answer["conversation_id"]) == []


def test_the_triage_sample_identifies_and_quotes_no_one(client, make_identity):
    import sys

    sys.path.insert(0, "scripts")
    from failure_sample import sample

    me = make_identity("exec", can_view_wac=1)
    sign_in(client, me)
    answer = ask(client, "top 3 accounts by paid pack units for FLOOBERTAX last quarter")
    good = ask(client, QUESTION)
    client.post("/api/feedback", json={"run_id": good["run_id"], "helpful": False,
                                       "reason": "wrong_number",
                                       "comment": f"my email is {me.email}"})
    withheld = json.dumps(sample(1, 500, include_comments=False), default=str)
    for secret in (me.user_id, me.email, "FLOOBERTAX", QUESTION):
        assert secret not in withheld, secret
    assert "wrong_number" in withheld
    assert answer["status"] == "clarify"
    shown = json.dumps(sample(1, 500, include_comments=True), default=str)
    assert me.email in shown            # only when an operator asks for comments
