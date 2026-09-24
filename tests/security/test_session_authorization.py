"""Authorization is re-evaluated on every request, including for stored material.

These tests run against a DISPOSABLE database (`pharma_analytics_authtest`,
built by `scripts/build_authtest_db.py`) because they change a user's role,
territory and pricing permission to simulate a real permission change. The
working database is never dropped, truncated or edited by the suite.

The identities here are created by the test and deleted afterwards. No
evaluator credential is read, rotated or used.

What is under test is the distinction between OWNERSHIP and CURRENT
AUTHORIZATION. A stored answer headline can contain a WAC amount
("Gross revenue: $250,766,926.42") and a conversation title is the question
text, so "the user who created it" is not a sufficient reason to hand it back
later. Every scenario below is a permission change between two requests.
"""

from __future__ import annotations

from tests.security.helpers import ask, has_money, sign_in

# ---------------------------------------------------------------------------
# 1. Pricing permission withdrawn
# ---------------------------------------------------------------------------

def test_losing_wac_hides_prior_pricing_history_and_new_pricing(
    client, make_identity
):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)

    answered = ask(client, "What is our total revenue this quarter?")
    conversation_id = answered["conversation_id"]
    assert answered["status"] == "answered"
    # The precondition for the test: the stored answer really does carry a price.
    assert has_money(answered), "expected a WAC-derived headline to store"

    assert client.get(f"/api/conversations/{conversation_id}").status_code == 200
    listed = client.get("/api/conversations").json()["conversations"]
    assert any(c["conversation_id"] == conversation_id for c in listed)

    user.change(can_view_wac=0)

    # Same session, same user, same conversation -- but no longer priced.
    after = client.get(f"/api/conversations/{conversation_id}")
    assert after.status_code == 404, "prior revenue answer still readable after losing WAC"

    listed_after = client.get("/api/conversations").json()["conversations"]
    assert not any(c["conversation_id"] == conversation_id for c in listed_after), (
        "the conversation title is still listed after losing WAC"
    )

    # And a new request cannot reach pricing either. The system is allowed to
    # answer with a labelled volume substitute; what it may not do is produce a
    # price, or substitute without saying so. (Whether a substitution is the
    # right response at all, rather than a clarification, is intent fidelity --
    # tracked separately as R08 and not asserted here.)
    fresh = ask(client, "What is our total revenue in dollars this quarter?")
    assert not has_money(fresh), "pricing reached a user who no longer has WAC"
    if fresh["status"] == "answered":
        notes = " ".join(fresh["answer"]["notes"])
        assert "restricted" in notes.lower(), (
            "volume was substituted for revenue without telling the user"
        )


def test_continuing_a_conversation_after_losing_wac_starts_fresh(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = ask(client, "What is our total revenue this quarter?")
    conversation_id = first["conversation_id"]

    user.change(can_view_wac=0)

    carried = ask(client, "Break that down by territory", conversation_id)
    assert carried["conversation_id"] != conversation_id, (
        "a plan from a higher permission level was carried across the change"
    )
    assert not has_money(carried)


# ---------------------------------------------------------------------------
# 2. Role change
# ---------------------------------------------------------------------------

def test_exec_becoming_director_loses_company_wide_history(
    client, make_identity, real_scopes
):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    company_wide = ask(client, "What are our top 5 accounts by pack units this quarter?")
    conversation_id = company_wide["conversation_id"]
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 200

    user.change(role="director", region_name=real_scopes[0]["region_name"],
                territory_name=None, can_view_wac=0)

    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "director"

    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404, (
        "company-wide history still readable as a Director"
    )
    listed = client.get("/api/conversations").json()["conversations"]
    assert not any(c["conversation_id"] == conversation_id for c in listed)


def test_exec_becoming_ram_loses_company_wide_history(
    client, make_identity, real_scopes
):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    conversation_id = ask(
        client, "What are our top 5 accounts by pack units this quarter?"
    )["conversation_id"]

    user.change(role="ram", territory_name=real_scopes[0]["territory_name"],
                region_name=real_scopes[0]["region_name"], can_view_wac=0)

    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert client.get("/api/conversations").json()["conversations"] == []


# ---------------------------------------------------------------------------
# 3. Territory move
# ---------------------------------------------------------------------------

def test_ram_moving_territory_loses_old_territory_history(
    client, make_identity, real_scopes
):
    old, new = real_scopes[0], real_scopes[1]
    user = make_identity("ram", territory=old["territory_name"],
                         region=old["region_name"])
    sign_in(client, user)

    answered = ask(client, "What are my top 5 accounts by pack units this quarter?")
    conversation_id = answered["conversation_id"]
    # The stored answer names the old territory in its scope note.
    assert old["territory_name"] in str(answered.get("answer", {}).get("scope_note", ""))
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 200

    user.change(territory_name=new["territory_name"], region_name=new["region_name"])

    assert client.get("/api/me").json()["user"]["scope"] != old["territory_name"]
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404, (
        "old-territory results still readable after the move"
    )
    listed = client.get("/api/conversations").json()["conversations"]
    assert not any(c["conversation_id"] == conversation_id for c in listed)


# ---------------------------------------------------------------------------
# 4. A different user
# ---------------------------------------------------------------------------

def test_another_user_cannot_read_the_conversation(client, make_identity, real_scopes):
    owner = make_identity("ram", territory=real_scopes[0]["territory_name"],
                          region=real_scopes[0]["region_name"])
    other = make_identity("ram", territory=real_scopes[0]["territory_name"],
                          region=real_scopes[0]["region_name"])

    sign_in(client, owner)
    conversation_id = ask(
        client, "What are my top 5 accounts by pack units this quarter?"
    )["conversation_id"]

    sign_in(client, other)
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert client.get("/api/conversations").json()["conversations"] == []

    # Nor may they append to it -- writing is authorized by the same rule.
    r = client.post("/api/ask", json={"question": "Break that down by quarter",
                                      "conversation_id": conversation_id})
    assert r.status_code == 404


def test_identical_scope_is_not_enough_without_ownership(
    client, make_identity, real_scopes
):
    """Two RAMs on the SAME territory still cannot read each other's threads.

    The fingerprint alone would match here; ownership is what separates them,
    so this proves the two checks are combined rather than one standing in for
    the other.
    """
    a = make_identity("ram", territory=real_scopes[0]["territory_name"],
                      region=real_scopes[0]["region_name"])
    b = make_identity("ram", territory=real_scopes[0]["territory_name"],
                      region=real_scopes[0]["region_name"])

    sign_in(client, a)
    conversation_id = ask(client, "What are my top 5 accounts this quarter?")[
        "conversation_id"]
    sign_in(client, b)
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404


# ---------------------------------------------------------------------------
# 5. The unchanged user keeps their history
# ---------------------------------------------------------------------------

def test_unchanged_user_keeps_their_history(client, make_identity, real_scopes):
    """The rule must withhold on a change, not withhold in general."""
    user = make_identity("ram", territory=real_scopes[0]["territory_name"],
                         region=real_scopes[0]["region_name"])
    sign_in(client, user)

    first = ask(client, "What are my top 5 accounts by pack units this quarter?")
    conversation_id = first["conversation_id"]
    follow_up = ask(client, "Break that down by quarter", conversation_id)
    assert follow_up["conversation_id"] == conversation_id, "a follow-up was reset"

    history = client.get(f"/api/conversations/{conversation_id}")
    assert history.status_code == 200
    turns = history.json()["turns"]
    assert len(turns) == 2
    assert turns[0]["seq"] < turns[1]["seq"]

    listed = client.get("/api/conversations").json()["conversations"]
    assert any(c["conversation_id"] == conversation_id for c in listed)

    # A fresh sign-in as the same unchanged user sees the same history.
    sign_in(client, user)
    again = client.get(f"/api/conversations/{conversation_id}")
    assert again.status_code == 200
    assert len(again.json()["turns"]) == 2
