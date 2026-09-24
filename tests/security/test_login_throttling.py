"""A password can no longer be guessed without limit or trace.

The identical failure message hides WHICH part of a sign-in was wrong, which
is right -- and on its own does nothing to stop an attacker simply trying
again, with nothing recorded to show that they had.

Counted per identity AND per source: per-identity alone lets one client spray
many accounts, per-source alone lets many clients share one target.
"""

from __future__ import annotations

import secrets

import pytest

from app.auth import identity


@pytest.fixture(autouse=True)
def clean_attempts(authtest_db):
    from app.db import auth_transaction

    def wipe():
        with auth_transaction() as cur:
            cur.execute("DELETE FROM app_auth.login_attempts")

    wipe()
    yield
    wipe()


def guess(email, times, ip="203.0.113.9"):
    outcomes = []
    for _ in range(times):
        try:
            identity.authenticate(email, "definitely-not-the-password", ip=ip)
            outcomes.append("ok")
        except identity.RateLimited:
            outcomes.append("limited")
        except identity.AuthenticationError:
            outcomes.append("rejected")
    return outcomes


def test_repeated_failures_for_one_identity_are_eventually_refused(make_identity):
    user = make_identity("exec", can_view_wac=1)
    outcomes = guess(user.email, identity.MAX_FAILURES_PER_EMAIL + 2)
    assert "limited" in outcomes, "a password could be guessed without limit"
    assert outcomes[0] == "rejected", "throttled before any attempt was made"


def test_the_limit_does_not_leak_whether_the_account_exists(make_identity):
    """A real and an unknown address must behave the same."""
    user = make_identity("exec", can_view_wac=1)
    real = guess(user.email, identity.MAX_FAILURES_PER_EMAIL + 2, ip="198.51.100.1")

    from app.db import auth_transaction
    with auth_transaction() as cur:
        cur.execute("DELETE FROM app_auth.login_attempts")

    unknown = guess(f"nobody-{secrets.token_hex(4)}@test.invalid",
                    identity.MAX_FAILURES_PER_EMAIL + 2, ip="198.51.100.2")
    assert real == unknown


def test_one_source_guessing_many_accounts_is_also_refused(make_identity):
    """Per-identity counting alone would let this through."""
    ip = "192.0.2.77"
    outcomes = []
    for _ in range(identity.MAX_FAILURES_PER_IP + 2):
        email = f"spray-{secrets.token_hex(4)}@test.invalid"
        outcomes += guess(email, 1, ip=ip)
    assert "limited" in outcomes, "one source could spray unlimited accounts"


def test_a_successful_sign_in_clears_the_budget(make_identity):
    """A user who mistypes twice and then succeeds is not held to it."""
    user = make_identity("exec", can_view_wac=1)
    guess(user.email, 3)

    session, principal = identity.authenticate(user.email, user.password,
                                               ip="203.0.113.9")
    assert principal.user_id == user.user_id

    # The next mistake starts from a clean slate rather than the old count.
    assert guess(user.email, 1) == ["rejected"]


def test_the_correct_password_still_works_below_the_limit(make_identity):
    user = make_identity("exec", can_view_wac=1)
    guess(user.email, identity.MAX_FAILURES_PER_EMAIL - 1)
    session, principal = identity.authenticate(user.email, user.password,
                                               ip="203.0.113.9")
    assert session.token


def test_no_password_is_ever_stored_in_the_attempt_log(make_identity):
    from app.db import auth_transaction

    user = make_identity("exec", can_view_wac=1)
    identity.authenticate(user.email, user.password, ip="203.0.113.9")
    guess(user.email, 2)

    with auth_transaction() as cur:
        cur.execute("SELECT * FROM app_auth.login_attempts")
        rows = [dict(r) for r in cur.fetchall()]
    assert rows
    blob = " ".join(str(v) for r in rows for v in r.values())
    assert user.password not in blob
    assert "definitely-not-the-password" not in blob
    # The IP is stored hashed, never in the clear.
    assert "203.0.113.9" not in blob


def test_the_http_layer_answers_429_not_401(client, make_identity):
    """A client that cannot tell the difference will keep hammering."""
    user = make_identity("exec", can_view_wac=1)
    client.cookies.clear()
    codes = []
    for _ in range(identity.MAX_FAILURES_PER_EMAIL + 2):
        codes.append(client.post("/api/login",
                                 json={"email": user.email, "password": "wrong"}).status_code)
    assert 429 in codes, codes
    assert codes[0] == 401


def test_old_attempts_are_purgeable(make_identity):
    user = make_identity("exec", can_view_wac=1)
    guess(user.email, 2)
    assert identity.purge_old_login_attempts(days=0) >= 2
