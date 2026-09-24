"""Sessions end when they are supposed to, under whatever cookie name is configured.

Same disposable database and same throwaway identities as
test_session_authorization.py: no evaluator credential is read or rotated, and
nothing here touches the working database.

The two properties under test are easy to assume and were both untrue:

  * Disabling an account blocked new logins but left every session already
    issued under it working until it expired on its own -- which is the
    opposite of what disabling is for.
  * `cookie_name` was honoured when setting and clearing the cookie but not
    when reading it, so configuring any name other than the default left
    every request unauthenticated.
"""

from __future__ import annotations

import os

import pytest

from tests.security.helpers import sign_in


# ---------------------------------------------------------------------------
# Disabling
# ---------------------------------------------------------------------------

def test_disabling_an_account_kills_sessions_already_issued(client, make_identity):
    from app.auth import identity as ident

    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    assert client.get("/api/me").status_code == 200

    ident.set_disabled(user.user_id, True)

    assert client.get("/api/me").status_code == 401, (
        "a session issued before the account was disabled still works"
    )
    assert client.post("/api/ask", json={"question": "revenue this quarter"}
                       ).status_code == 401

    # And the account cannot simply sign in again.
    r = client.post("/api/login",
                    json={"email": user.email, "password": user.password})
    assert r.status_code == 401


def test_resolve_rejects_a_disabled_credential_directly(client, make_identity):
    """The check belongs in resolve(), not only in the revocation side effect.

    Disabling revokes outstanding sessions, so a test that only disables would
    pass even if resolve() ignored the flag. This one re-issues a session
    AFTER disabling, by writing it the way authenticate() does, to prove
    resolve() itself refuses.
    """
    import hashlib
    import secrets
    from datetime import datetime, timedelta, timezone

    from app.auth import identity as ident
    from app.db import auth_transaction

    # An assigned identity, so that a None from resolve() can only mean the
    # disabled check fired -- an unassigned RAM is refused for a different
    # reason and would make this test pass for the wrong one.
    user = make_identity("exec", can_view_wac=1)
    ident.set_disabled(user.user_id, True)

    token = secrets.token_urlsafe(32)
    with auth_transaction() as cur:
        cur.execute(
            "INSERT INTO app_auth.sessions (token_hash, user_id, expires_at) "
            "VALUES (%s, %s, %s)",
            (hashlib.sha256(token.encode()).hexdigest(), user.user_id,
             datetime.now(timezone.utc) + timedelta(hours=1)),
        )

    assert ident.resolve(token) is None, "resolve() honoured a disabled credential"

    ident.set_disabled(user.user_id, False)
    assert ident.resolve(token) is not None, (
        "re-enabling did not restore the still-valid session"
    )


def test_rotating_a_password_revokes_outstanding_sessions(client, make_identity):
    from app.auth import identity as ident

    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    assert client.get("/api/me").status_code == 200

    ident.set_credential(user.user_id, "a-new-password-" + os.urandom(4).hex())

    assert client.get("/api/me").status_code == 401, (
        "the old session survived a password rotation"
    )


def test_logout_revokes_the_token_not_just_the_cookie(client, make_identity):
    from app.auth import identity as ident

    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    token = client.cookies.get("pac_session")
    assert token

    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401
    # The cookie is gone from the jar; the token must be dead server-side too.
    assert ident.resolve(token) is None, "logout cleared the cookie but kept the session"


# ---------------------------------------------------------------------------
# Cookie name
# ---------------------------------------------------------------------------

@pytest.fixture
def renamed_cookie_client(authtest_db):
    """A client whose app is configured with a non-default cookie name."""
    from fastapi.testclient import TestClient

    from app.config import get_settings

    original = os.environ.get("PAC_COOKIE_NAME")
    os.environ["PAC_COOKIE_NAME"] = "custom_session_name"
    os.environ["PAC_COOKIE_SECURE"] = "false"
    get_settings.cache_clear()
    try:
        from app.api.main import app

        with TestClient(app) as c:
            yield c
    finally:
        if original is None:
            os.environ.pop("PAC_COOKIE_NAME", None)
        else:
            os.environ["PAC_COOKIE_NAME"] = original
        get_settings.cache_clear()


def test_a_nondefault_cookie_name_is_used_by_login_resolve_and_logout(
    renamed_cookie_client, make_identity
):
    c = renamed_cookie_client
    user = make_identity("exec", can_view_wac=1)

    c.cookies.clear()
    r = c.post("/api/login", json={"email": user.email, "password": user.password})
    assert r.status_code == 200
    assert "custom_session_name" in r.cookies, "login ignored the configured name"
    assert "pac_session" not in r.cookies

    # Reading: the request must authenticate using that cookie.
    me = c.get("/api/me")
    assert me.status_code == 200, "the configured cookie name was not read back"
    assert me.json()["user"]["email"] == user.email

    # Clearing.
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").status_code == 401


def test_the_default_cookie_name_is_not_accepted_when_renamed(
    renamed_cookie_client, make_identity
):
    """A token presented under the old name must not authenticate."""
    c = renamed_cookie_client
    user = make_identity("exec", can_view_wac=1)

    c.cookies.clear()
    c.post("/api/login", json={"email": user.email, "password": user.password})
    token = c.cookies.get("custom_session_name")
    assert token

    c.cookies.clear()
    c.cookies.set("pac_session", token)
    assert c.get("/api/me").status_code == 401
