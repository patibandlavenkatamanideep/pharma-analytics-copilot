"""Clarification that survives a restart, idempotent turns, one run at a time.

Review finding 4, over real HTTP and the real conversation tables:

* "Riverside Clinic is the name of 2 accounts -- which one?" followed by "the
  second one" resolves against the choices that were SHOWN, after a restart,
  and only if the choice is still within the caller's access.
* A retried request with the same Idempotency-Key gets the committed outcome
  back instead of a second turn; the same key with a different question is a
  conflict; the replay is withheld if access changed since.
* One live run per conversation: an overlapping request is refused as busy
  rather than planned against state that is about to change, and a lease
  left by a crashed worker is reclaimed.
* A failed save is reported as a failed save.
"""

from __future__ import annotations

import secrets

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security

TWIN = "Pactest Twin Clinic"


@pytest.fixture
def twins(authtest_db):
    """Two standalone clinics with one name, in different states. The
    disposable data has no shared names, so the test makes its own and
    removes them."""
    from app.analytics.mentions import clear_caches
    from app.db import owner_transaction

    suffix = secrets.token_hex(3).upper()
    rows = [(f"SA-TW1-{suffix}", "TX", "75201"), (f"SA-TW2-{suffix}", "OR", "97201")]
    with owner_transaction() as cur:
        for org_id, state, zip_code in rows:
            cur.execute(
                "INSERT INTO organizations (org_id, org_name, org_type, org_status, state, zip) "
                "VALUES (%s, %s, 'Facility', 'Active', %s, %s)",
                (org_id, TWIN, state, zip_code))
    clear_caches()
    yield [r[0] for r in rows]
    with owner_transaction() as cur:
        cur.execute("DELETE FROM organizations WHERE org_id = ANY(%s)", ([r[0] for r in rows],))
    clear_caches()


def ask(client, question, conversation_id=None, key=None):
    body = {"question": question, "include_sql": True}
    if conversation_id:
        body["conversation_id"] = conversation_id
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/ask", json=body, headers=headers)


def turns(conversation_id):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT seq, question, status FROM app_conv.turns "
                    "WHERE conversation_id = %s ORDER BY seq", (conversation_id,))
        return [dict(r) for r in cur.fetchall()]


def restart_process_state():
    """What a restart loses: pools, caches, the pipeline object. What it
    must not lose is the database."""
    import app.api.main as api
    from app.analytics.entities import clear_caches
    from app.db import close_pools
    from app.llm.planner import build_planner
    from app.pipeline import Pipeline
    from app.config import get_settings

    close_pools()
    clear_caches()
    api._pipeline = Pipeline(build_planner(get_settings()))


# ---------------------------------------------------------------------------
# Clarification that survives a restart
# ---------------------------------------------------------------------------

def test_a_shared_name_is_asked_about_with_the_real_choices(client, make_identity, twins):
    sign_in(client, make_identity("exec", can_view_wac=1))
    r = ask(client, f"What was the volume for {TWIN} in the last 3 months?")
    body = r.json()

    assert body["status"] == "clarify", body
    assert {c["id"] for c in body["choices"]} == set(twins)
    assert {c["detail"].split(", ")[-1] for c in body["choices"]} == {"TX", "OR"}


def test_the_choices_are_shown_in_the_same_order_every_time(client, make_identity, twins):
    """"The second one" must mean one account, not whichever the database
    grouped second on this occasion."""
    sign_in(client, make_identity("exec", can_view_wac=1))
    orders = {tuple(c["id"] for c in ask(
        client, f"What was the volume for {TWIN} in the last 3 months?").json()["choices"])
        for _ in range(3)}
    assert len(orders) == 1


#: How each reply picks from the choices AS SHOWN -- by position, or by the
#: one detail that tells them apart. Never by an assumed order.
PICKERS = {
    "the second one": lambda shown: shown[1]["id"],
    "2": lambda shown: shown[1]["id"],
    "the first one": lambda shown: shown[0]["id"],
    "the one in Texas": lambda shown: next(c["id"] for c in shown
                                           if c["detail"].endswith("TX")),
}


@pytest.mark.parametrize("reply", sorted(PICKERS))
def test_the_reply_resolves_against_what_was_shown_after_a_restart(
        client, make_identity, twins, reply):
    sign_in(client, make_identity("exec", can_view_wac=1))
    first = ask(client, f"What was the volume for {TWIN} in the last 3 months?").json()
    expected = PICKERS[reply](first["choices"])

    restart_process_state()

    second = ask(client, reply, first["conversation_id"]).json()
    assert second["status"] == "answered", second
    assert second["plan"]["filters"]["account_ids"] == [expected]
    # The clarification is resolved, and the history records both turns.
    assert [t["status"] for t in turns(first["conversation_id"])] == ["clarify", "answered"]


def test_a_reply_that_is_not_a_choice_is_a_new_question(client, make_identity, twins):
    """'What about the top 2 accounts?' contains a 2 without choosing."""
    sign_in(client, make_identity("exec", can_view_wac=1))
    first = ask(client, f"What was the volume for {TWIN} in the last 3 months?").json()
    second = ask(client, "What are our top 2 accounts by volume this quarter?",
                 first["conversation_id"]).json()
    assert second["status"] == "answered"
    assert not set(twins) & set(second["plan"]["filters"].get("account_ids") or []), (
        "the 2 was read as choosing the second clinic")

    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT status FROM app_conv.clarifications WHERE conversation_id = %s",
                    (first["conversation_id"],))
        assert [r["status"] for r in cur.fetchall()] == ["superseded"]


def test_a_choice_no_longer_available_is_not_honoured(client, make_identity, twins):
    """A stored option is not a grant. Between the question and the answer
    the second clinic leaves the caller's view."""
    from app.analytics.mentions import clear_caches
    from app.db import owner_transaction

    sign_in(client, make_identity("exec", can_view_wac=1))
    first = ask(client, f"What was the volume for {TWIN} in the last 3 months?").json()
    shown_second = first["choices"][1]["id"]
    with owner_transaction() as cur:
        cur.execute("DELETE FROM organizations WHERE org_id = %s", (shown_second,))
    clear_caches()

    second = ask(client, "the second one", first["conversation_id"]).json()
    assert second["status"] == "clarify"
    assert "no longer available" in second["message"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_a_retry_with_the_same_key_replays_one_committed_turn(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    key = "k-" + secrets.token_hex(8)
    first = ask(client, "What is our total volume this quarter?", key=key).json()
    again = ask(client, "What is our total volume this quarter?", key=key).json()

    assert again["replayed"] is True
    assert again["conversation_id"] == first["conversation_id"]
    assert again["answer"]["headline"] == first["answer"]["headline"]
    assert len(turns(first["conversation_id"])) == 1, "the retry recorded a second turn"


def test_the_same_key_for_a_different_question_is_a_conflict(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    key = "k-" + secrets.token_hex(8)
    ask(client, "What is our total volume this quarter?", key=key)
    r = ask(client, "What is our total volume last quarter?", key=key)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_reused"


def test_a_replay_is_withheld_after_access_changes(client, make_identity):
    """The stored answer carries revenue. The user has since lost pricing."""
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    key = "k-" + secrets.token_hex(8)
    first = ask(client, "What is our total revenue this quarter?", key=key).json()
    assert "$" in first["answer"]["headline"]

    user.change(can_view_wac=0)
    r = ask(client, "What is our total revenue this quarter?", key=key)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "access_changed"
    assert "$" not in r.text


def test_another_user_cannot_replay_or_read_a_run(client, make_identity):
    alice, bob = make_identity("exec", can_view_wac=1), make_identity("exec", can_view_wac=1)
    key = "k-" + secrets.token_hex(8)
    sign_in(client, alice)
    first = ask(client, "What is our total revenue this quarter?", key=key).json()

    sign_in(client, bob)
    theirs = ask(client, "What is our total revenue this quarter?", key=key).json()
    assert theirs.get("replayed") is not True, "a key replayed across users"
    assert theirs["conversation_id"] != first["conversation_id"]
    assert client.get(f"/api/runs/{first['run_id']}").status_code == 404


# ---------------------------------------------------------------------------
# One run at a time, and a lease that expires
# ---------------------------------------------------------------------------

def _plant_run(conversation_id, owner, *, live: bool):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute(
            "INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, payload_hash, "
            "  scope_fingerprint, base_revision, status, lease_expires_at, expires_at) "
            "VALUES (%s, %s, %s, 'x', 'x', 0, 'running', now() + %s::interval, "
            "        now() + interval '1 day')",
            ("r_plant_" + secrets.token_hex(4), conversation_id, owner,
             "5 minutes" if live else "-1 second"))


def test_an_overlapping_request_is_refused_as_busy(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = ask(client, "What is our total volume this quarter?").json()
    _plant_run(first["conversation_id"], user.user_id, live=True)

    r = ask(client, "And last quarter?", first["conversation_id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "conversation_busy"


def test_a_lease_left_by_a_crashed_worker_is_reclaimed(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = ask(client, "What is our total volume this quarter?").json()
    _plant_run(first["conversation_id"], user.user_id, live=False)

    r = ask(client, "What is our total volume last quarter?", first["conversation_id"])
    assert r.status_code == 200 and r.json()["status"] == "answered"


# ---------------------------------------------------------------------------
# A save that fails says so
# ---------------------------------------------------------------------------

def test_a_failed_save_is_reported_not_swallowed(client, make_identity, monkeypatch):
    import app.pipeline as pipeline_module

    def broken(*args, **kwargs):
        raise RuntimeError("conversation store unavailable")

    monkeypatch.setattr(pipeline_module, "finalise", broken)
    sign_in(client, make_identity("exec", can_view_wac=1))
    body = ask(client, "What is our total volume this quarter?").json()

    assert body["status"] == "answered", "the answer itself was fine"
    assert body["persistence"] == "failed"
    assert turns(body["conversation_id"]) == []


def test_a_saved_turn_says_so(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    body = ask(client, "What is our total volume this quarter?").json()
    assert body["persistence"] == "saved"
    assert len(turns(body["conversation_id"])) == 1
