"""Idle expiry and token rotation.

A session had one bound: twelve hours from sign-in. A laptop left open kept a
working session all day, and a token leaked at minute one stayed valid for
all twelve hours. Now:

* idle -- unused for the idle window (30 min) means dead, whatever the
  absolute expiry says;
* rotation -- a token older than the rotation window (15 min) is replaced on
  its next use; the old one keeps working for a short grace so requests in
  flight finish, then stops;
* the absolute expiry is fixed at sign-in and never extended -- not by
  activity, not by rotation.

Time is moved by editing the session row in the disposable database rather
than by waiting.
"""

from __future__ import annotations

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security


def session_row(token):
    from app.auth.identity import _token_hash
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT * FROM app_auth.sessions WHERE token_hash = %s", (_token_hash(token),))
        return cur.fetchone()


def shift(token, **intervals):
    """Move a session's clocks into the past: shift(t, last_seen_at='31 minutes')."""
    from app.auth.identity import _token_hash
    from app.db import owner_transaction
    sets = ", ".join(f"{col} = now() - %s::interval" for col in intervals)
    with owner_transaction() as cur:
        cur.execute(f"UPDATE app_auth.sessions SET {sets} WHERE token_hash = %s",
                    (*intervals.values(), _token_hash(token)))


def cookie(client):
    from app.config import get_settings
    return client.cookies.get(get_settings().cookie_name)


def test_an_idle_session_is_dead_inside_its_absolute_window(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    token = cookie(client)
    assert client.get("/api/me").status_code == 200

    shift(token, last_seen_at="31 minutes", rotated_at="1 minute")
    assert session_row(token)["expires_at"] is not None
    assert client.get("/api/me").status_code == 401


def test_activity_keeps_a_session_alive(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    token = cookie(client)
    shift(token, last_seen_at="29 minutes", rotated_at="1 minute")
    assert client.get("/api/me").status_code == 200
    seen = session_row(token)["last_seen_at"]
    shift(token, rotated_at="1 minute")                # leave last_seen_at as touched
    assert session_row(token)["last_seen_at"] == seen
    assert client.get("/api/me").status_code == 200


def test_an_old_token_is_rotated_on_use_without_extending_the_session(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    old = cookie(client)
    expiry = session_row(old)["expires_at"]
    shift(old, rotated_at="16 minutes")

    r = client.get("/api/me")
    new = cookie(client)

    assert r.status_code == 200
    assert new and new != old, "the token was not rotated"
    assert session_row(new)["expires_at"] == expiry, "rotation extended the session"
    assert session_row(old)["superseded_at"] is not None


def test_a_rotated_out_token_works_briefly_then_stops(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    old = cookie(client)
    shift(old, rotated_at="16 minutes")
    client.get("/api/me")                               # rotates

    from app.config import get_settings
    name = get_settings().cookie_name
    client.cookies.set(name, old)
    assert client.get("/api/me").status_code == 200     # in-flight grace

    shift(old, superseded_at="31 seconds")
    client.cookies.set(name, old)
    assert client.get("/api/me").status_code == 401


def test_only_one_of_two_racing_requests_rotates(client, make_identity):
    from app.auth import identity

    sign_in(client, make_identity("exec", can_view_wac=1))
    old = cookie(client)
    first, second = identity.rotate(old), identity.rotate(old)
    assert first is not None and second is None


def test_logout_still_ends_the_session_immediately(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    token = cookie(client)
    client.post("/api/logout")
    from app.config import get_settings
    client.cookies.set(get_settings().cookie_name, token)
    assert client.get("/api/me").status_code == 401
