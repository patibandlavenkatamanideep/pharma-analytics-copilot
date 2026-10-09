"""Single sign-on, against a provider that checks what a real one checks.

The provider here is a fake, in process, behind httpx.MockTransport -- no
network and no external service. It is not a stub that says yes: it binds
each authorization code to the PKCE challenge and nonce taken from the real
authorization URL our code built, refuses a token request whose verifier
does not match the challenge, and signs ID tokens with a real RSA key
published through a real JWKS document. So the tests exercise Authlib's
exchange and joserfc's verification as they run in production.

What it cannot establish: interoperability with a specific commercial
provider, or that provider's MFA policy. Those need a registered client.
"""

from __future__ import annotations

import hashlib
import secrets
import time

import httpx2 as httpx
import pytest
from joserfc.jwk import RSAKey

from tests.security.fake_idp import CLIENT, ISSUER, REDIRECT, FakeIdP
from tests.security.helpers import sign_in  # noqa: F401  (fixture module import)

pytestmark = pytest.mark.security


@pytest.fixture
def idp(client, monkeypatch):
    import app.api.main as api
    from app.auth.oidc import Provider
    from app.config import get_settings

    for name, value in {"PAC_OIDC_ENABLED": "true", "PAC_OIDC_ISSUER": ISSUER,
                        "PAC_OIDC_CLIENT_ID": CLIENT, "PAC_OIDC_REDIRECT_URI": REDIRECT}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    fake = FakeIdP()
    api._oidc = Provider(transport=httpx.MockTransport(fake.handler))
    yield fake
    api._oidc = None
    monkeypatch.undo()
    get_settings.cache_clear()


def link(user, sub):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("INSERT INTO app_auth.identities (issuer, subject, user_id) VALUES (%s, %s, %s)",
                    (ISSUER, sub, user.user_id))


def sso(client, idp, *, sub, email=None, verified=False, next_path=None):
    client.cookies.clear()
    start = client.get("/api/auth/oidc/start" + (f"?next={next_path}" if next_path else ""),
                       follow_redirects=False)
    assert start.status_code == 302
    code, state = idp.authorize(start.headers["location"], sub=sub, email=email,
                                verified=verified)
    return client.get(f"/api/auth/oidc/callback?code={code}&state={state}",
                      follow_redirects=False), code, state


# ---------------------------------------------------------------------------

def test_a_linked_identity_signs_in_and_gets_an_ordinary_session(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-alice")
    r, _, _ = sso(client, idp, sub="sub-alice")
    assert r.status_code == 303 and r.headers["location"] == "/"
    me = client.get("/api/me")
    assert me.status_code == 200 and me.json()["user"]["email"] == user.email


def test_a_replayed_callback_is_refused(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-replay")
    first, code, state = sso(client, idp, sub="sub-replay")
    assert first.status_code == 303
    again = client.get(f"/api/auth/oidc/callback?code={code}&state={state}",
                       follow_redirects=False)
    assert again.status_code == 400 and again.json()["detail"]["code"] == "invalid_state"


def test_a_forged_state_is_refused(client, idp):
    r = client.get("/api/auth/oidc/callback?code=x&state=made-up", follow_redirects=False)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_state"


def test_an_expired_attempt_is_refused(client, idp, make_identity):
    from app.db import owner_transaction
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-late")
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    code, state = idp.authorize(start.headers["location"], sub="sub-late")
    with owner_transaction() as cur:
        cur.execute("UPDATE app_auth.oidc_pending SET expires_at = now() - interval '1 second'")
    r = client.get(f"/api/auth/oidc/callback?code={code}&state={state}", follow_redirects=False)
    assert r.json()["detail"]["code"] == "invalid_state"


def test_a_wrong_pkce_verifier_is_refused_by_the_provider(client, idp, make_identity):
    """An intercepted code is useless without the verifier only the server
    holds. Simulated by corrupting the stored verifier."""
    from app.db import owner_transaction
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-pkce")
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    code, state = idp.authorize(start.headers["location"], sub="sub-pkce")
    with owner_transaction() as cur:
        cur.execute("UPDATE app_auth.oidc_pending SET code_verifier = 'not-the-verifier'")
    r = client.get(f"/api/auth/oidc/callback?code={code}&state={state}", follow_redirects=False)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "token_rejected"


@pytest.mark.parametrize("override,why", [
    ({"nonce": "someone-elses-nonce"}, "nonce"),
    ({"iss": "https://other-idp.test"}, "issuer"),
    ({"aud": "another-client"}, "audience"),
    ({"exp": int(time.time()) - 3600}, "expired"),
    ({"aud": [CLIENT, "another"], "azp": "another"}, "authorised party"),
])
def test_a_token_that_fails_a_claim_check_is_refused(client, idp, make_identity, override, why):
    user = make_identity("exec", can_view_wac=1)
    sub = "sub-claims-" + secrets.token_hex(3)
    link(user, sub)
    idp.claim_overrides = override
    r, _, _ = sso(client, idp, sub=sub)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_token", why
    assert client.get("/api/me").status_code == 401


def test_a_token_signed_by_another_key_is_refused(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-forged")
    idp.sign_with = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)
    r, _, _ = sso(client, idp, sub="sub-forged")
    assert r.json()["detail"]["code"] == "invalid_token"


def test_a_symmetric_algorithm_is_refused(client, idp, make_identity):
    """HS256 signed with a key anyone can see is the classic confusion."""
    from joserfc.jwk import OctKey
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-hs")
    idp.sign_with = OctKey.import_key(secrets.token_bytes(32))
    idp.header_overrides = {"alg": "HS256"}
    r, _, _ = sso(client, idp, sub="sub-hs")
    assert r.json()["detail"]["code"] == "invalid_token"


def test_a_rotated_signing_key_is_picked_up(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-rotate")
    assert sso(client, idp, sub="sub-rotate")[0].status_code == 303   # keys now cached
    idp.signing = RSAKey.generate_key(2048, parameters={"kid": "k2"}, private=True)
    idp.published = [idp.signing]
    idp.header_overrides = {"kid": "k2"}
    assert sso(client, idp, sub="sub-rotate")[0].status_code == 303


def test_an_unlinked_identity_is_not_signed_in_by_email(client, idp, make_identity):
    """Email is not the key: a valid identity whose email matches an account
    does not become that account unless linking is enabled."""
    user = make_identity("exec", can_view_wac=1)
    r, _, _ = sso(client, idp, sub="sub-stranger", email=user.email, verified=True)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_linked"


def test_opt_in_linking_needs_a_provider_verified_email(client, idp, make_identity, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("PAC_OIDC_LINK_BY_VERIFIED_EMAIL", "true")
    get_settings.cache_clear()
    idp_settings_user = make_identity("exec", can_view_wac=1)

    unverified, _, _ = sso(client, idp, sub="sub-u1", email=idp_settings_user.email,
                           verified=False)
    assert unverified.status_code == 403
    verified, _, _ = sso(client, idp, sub="sub-v1", email=idp_settings_user.email, verified=True)
    assert verified.status_code == 303
    # Linked by subject now: a later email change at the provider is irrelevant.
    again, _, _ = sso(client, idp, sub="sub-v1", email="changed@elsewhere.test", verified=True)
    assert again.status_code == 303


def test_a_disabled_account_cannot_sign_in_through_the_provider(client, idp, make_identity):
    from app.auth.identity import set_disabled
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-disabled")
    set_disabled(user.user_id, True)
    r, _, _ = sso(client, idp, sub="sub-disabled")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "disabled"


def test_only_a_relative_next_path_is_honoured(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-next")
    r, _, _ = sso(client, idp, sub="sub-next", next_path="https://evil.example/")
    assert r.headers["location"] == "/"
    r, _, _ = sso(client, idp, sub="sub-next", next_path="//evil.example/x")
    assert r.headers["location"] == "/"


def test_an_sso_only_user_cannot_use_a_password(client, idp, make_identity):
    """The credentials row created for an SSO user has a hash nothing
    verifies; sign-in by password stays impossible."""
    from app.db import owner_transaction
    user = make_identity("exec", can_view_wac=1)
    with owner_transaction() as cur:
        cur.execute("DELETE FROM app_auth.credentials WHERE user_id = %s", (user.user_id,))
    link(user, "sub-ssoonly")
    assert sso(client, idp, sub="sub-ssoonly")[0].status_code == 303
    r = client.post("/api/login", json={"email": user.email, "password": "!"})
    assert r.status_code == 401


def test_sso_is_absent_unless_configured(client):
    assert client.get("/api/auth/methods").json() == {"password": True, "oidc": False}
    assert client.get("/api/auth/oidc/start", follow_redirects=False).status_code == 404


# -- the callback must come from the browser that started the sign-in --------------
#
# Review of 1 October 2026, R1: login CSRF. State, nonce and verifier were
# stored server-side and found by `state` alone, so a callback obtained in
# one browser completed in any other. The first four reproduced it on the
# unmodified code (evidence/runs/r3-r1-reproduced.json); each sign-in is now
# bound to a secret held in the starting browser's cookie.

def another_browser():
    """A second browser: the same application, its own empty cookie jar."""
    from fastapi.testclient import TestClient

    import app.api.main as api
    return TestClient(api.app)


def started(client, idp, sub):
    client.cookies.clear()
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    assert start.status_code == 302
    code, state = idp.authorize(start.headers["location"], sub=sub)
    return start, code, state


def callback(browser, code, state):
    return browser.get(f"/api/auth/oidc/callback?code={code}&state={state}",
                       follow_redirects=False)


def test_a_callback_carried_to_another_browser_does_not_sign_it_in(client, idp, make_identity):
    """The review's reproduction. Browser A starts a sign-in and authenticates
    as the ATTACKER; browser B -- the victim, with no cookies and no sign-in
    of its own -- is sent A's callback. B must not end up signed in as the
    attacker, and the code must not even be redeemed."""
    attacker = make_identity("exec", can_view_wac=1)
    link(attacker, "sub-attacker")
    _, code, state = started(client, idp, "sub-attacker")
    victim = another_browser()
    r = callback(victim, code, state)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "browser_mismatch"
    assert "set-cookie" not in r.headers
    assert victim.get("/api/me").status_code == 401
    assert code in idp.codes, "the code was redeemed before the browser was checked"
    # Refusing B did not use up A's sign-in.
    assert callback(client, code, state).status_code == 303


def test_a_callback_without_the_binding_cookie_is_refused(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-nocookie")
    _, code, state = started(client, idp, "sub-nocookie")
    client.cookies.clear()
    r = callback(client, code, state)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "browser_mismatch"
    assert client.get("/api/me").status_code == 401


def test_a_callback_with_another_browsers_binding_is_refused(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-wrongcookie")
    _, code, state = started(client, idp, "sub-wrongcookie")
    client.cookies.clear()
    client.cookies.set("pac_oidc", secrets.token_urlsafe(32))
    r = callback(client, code, state)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "browser_mismatch"


def test_starting_a_sign_in_sets_an_httponly_binding_cookie(client, idp):
    client.cookies.clear()
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    cookie = start.headers.get("set-cookie", "")
    assert cookie.startswith("pac_oidc="), cookie
    attributes = [a.strip().lower() for a in cookie.split(";")]
    assert {"httponly", "samesite=lax", "path=/", "max-age=600"} <= set(attributes)


def test_over_https_the_binding_cookie_is_secure_and_host_prefixed(client, idp, monkeypatch):
    """`__Host-` makes the browser refuse the cookie unless it is Secure,
    Path=/ and host-only, so a sibling subdomain cannot plant a binding."""
    from app.config import get_settings
    monkeypatch.setenv("PAC_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    cookie = start.headers.get("set-cookie", "")
    attributes = [a.strip().lower() for a in cookie.split(";")]
    assert cookie.startswith("__Host-pac_oidc="), cookie
    assert {"httponly", "secure", "samesite=lax", "path=/", "max-age=600"} <= set(attributes)
    assert not any(a.startswith("domain=") for a in attributes)


def test_the_binding_is_stored_only_as_a_hash(client, idp):
    from app.db import owner_transaction
    client.cookies.clear()
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    secret = start.cookies["pac_oidc"]
    with owner_transaction() as cur:
        cur.execute("SELECT * FROM app_auth.oidc_pending WHERE binding_hash = %s",
                    (hashlib.sha256(secret.encode()).hexdigest(),))
        rows = cur.fetchall()
    assert len(rows) == 1
    assert secret not in repr(dict(rows[0]))


def test_two_sign_ins_started_in_one_browser_can_both_finish(client, idp, make_identity):
    """Tabs, deliberately: a start in a browser with an attempt already
    pending shares its binding, so neither tab invalidates the other."""
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-tabs")
    client.cookies.clear()
    first = client.get("/api/auth/oidc/start", follow_redirects=False)
    second = client.get("/api/auth/oidc/start", follow_redirects=False)
    assert first.cookies["pac_oidc"] == second.cookies["pac_oidc"]
    code1, state1 = idp.authorize(first.headers["location"], sub="sub-tabs")
    code2, state2 = idp.authorize(second.headers["location"], sub="sub-tabs")
    later = callback(client, code2, state2)
    assert later.status_code == 303
    assert not cleared(later), "the binding was cleared while the other tab was pending"
    earlier = callback(client, code1, state1)
    assert earlier.status_code == 303
    assert cleared(earlier), "nothing is pending any more, so the binding should go"


def cleared(response) -> bool:
    return any(c.startswith("pac_oidc=") and "max-age=0" in c.lower()
               for c in response.headers.get_list("set-cookie"))


def test_a_fresh_browser_gets_a_fresh_binding(client, idp):
    """A binding is not reused once nothing is pending under it: a start
    after the attempt ended mints a new secret."""
    from app.db import owner_transaction
    client.cookies.clear()
    old = client.get("/api/auth/oidc/start", follow_redirects=False).cookies["pac_oidc"]
    with owner_transaction() as cur:
        cur.execute("DELETE FROM app_auth.oidc_pending WHERE binding_hash = %s",
                    (hashlib.sha256(old.encode()).hexdigest(),))
    new = client.get("/api/auth/oidc/start", follow_redirects=False).cookies["pac_oidc"]
    assert new != old


def test_a_malformed_binding_cookie_is_replaced_not_trusted(client, idp):
    client.cookies.clear()
    client.cookies.set("pac_oidc", "chosen-by-someone-else")
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    assert start.cookies["pac_oidc"] != "chosen-by-someone-else"


def test_a_cancelled_sign_in_cannot_be_finished_afterwards(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-cancel")
    _, code, state = started(client, idp, "sub-cancel")
    declined = client.get(f"/api/auth/oidc/callback?error=access_denied&state={state}",
                          follow_redirects=False)
    assert declined.status_code == 400
    assert declined.json()["detail"]["code"] == "provider_declined"
    after = callback(client, code, state)
    assert after.status_code == 400 and after.json()["detail"]["code"] == "invalid_state"
    assert client.get("/api/me").status_code == 401


def test_another_browser_cannot_cancel_a_sign_in(client, idp, make_identity):
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-nocancel")
    _, code, state = started(client, idp, "sub-nocancel")
    another_browser().get(f"/api/auth/oidc/callback?error=access_denied&state={state}",
                          follow_redirects=False)
    assert callback(client, code, state).status_code == 303


def test_an_expired_attempt_is_refused_even_from_the_right_browser(client, idp, make_identity):
    from app.db import owner_transaction
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-expired-bound")
    _, code, state = started(client, idp, "sub-expired-bound")
    with owner_transaction() as cur:
        cur.execute("UPDATE app_auth.oidc_pending SET expires_at = now() - interval '1 second' "
                    "WHERE state_hash = %s", (hashlib.sha256(state.encode()).hexdigest(),))
    r = callback(client, code, state)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_state"
    assert code in idp.codes, "an expired attempt still redeemed its code"


def test_the_previous_releases_attempts_still_insert_and_are_never_completed(client, idp,
                                                                             make_identity):
    """Rollback and rolling deploys. The previous release inserts attempts
    without a binding: that must still work on this schema (a NOT NULL
    column would break its SSO start), and this release must refuse to
    complete such an attempt -- before the code is redeemed."""
    from app.db import owner_transaction
    user = make_identity("exec", can_view_wac=1)
    link(user, "sub-legacy")
    with owner_transaction() as cur:                     # the previous release's insert
        cur.execute(
            "INSERT INTO app_auth.oidc_pending (state_hash, nonce, code_verifier, "
            "  redirect_after, expires_at) "
            "VALUES (%s, 'n', 'v', '/', now() + make_interval(secs => 600))",
            (hashlib.sha256(secrets.token_bytes(8)).hexdigest(),))
    _, code, state = started(client, idp, "sub-legacy")
    with owner_transaction() as cur:                     # as if that release had started it
        cur.execute("UPDATE app_auth.oidc_pending SET binding_hash = NULL WHERE state_hash = %s",
                    (hashlib.sha256(state.encode()).hexdigest(),))
    r = callback(client, code, state)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "browser_mismatch"
    assert code in idp.codes and client.get("/api/me").status_code == 401
