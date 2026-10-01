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

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from tests.security.helpers import sign_in  # noqa: F401  (fixture module import)

pytestmark = pytest.mark.security

ISSUER = "https://idp.test"
CLIENT = "pac-client"
REDIRECT = "http://testserver/api/auth/oidc/callback"


class FakeIdP:
    def __init__(self):
        self.signing = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"},
                                           private=True)
        self.published = [self.signing]
        self.codes: dict[str, dict] = {}
        self.claim_overrides: dict = {}
        self.sign_with = None            # a different key, to forge
        self.header_overrides: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"})
        if path == "/jwks":
            return httpx.Response(200, json=KeySet(self.published).as_dict(private=False))
        if path == "/token":
            form = parse_qs(request.content.decode())
            code = form.get("code", [""])[0]
            grant = self.codes.pop(code, None)                       # single use
            verifier = form.get("code_verifier", [""])[0]
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if grant is None or challenge != grant["challenge"] \
                    or form.get("redirect_uri", [""])[0] != REDIRECT:
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": CLIENT, "sub": grant["sub"], "iat": now,
                      "exp": now + 300, "nonce": grant["nonce"],
                      "email": grant.get("email"), "email_verified": grant.get("verified", False),
                      **self.claim_overrides}
            header = {"alg": "RS256", "kid": "k1", **self.header_overrides}
            token = jwt.encode(header, {k: v for k, v in claims.items() if v is not None},
                               self.sign_with or self.signing)
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer",
                                             "id_token": token})
        return httpx.Response(404)

    def authorize(self, url: str, *, sub: str, email: str | None = None,
                  verified: bool = False) -> tuple[str, str]:
        """What the provider does after the user authenticates: return a
        code bound to THIS request's PKCE challenge and nonce."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256", "PKCE missing or not S256"
        assert query["client_id"] == CLIENT and query["redirect_uri"] == REDIRECT
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": query["code_challenge"], "nonce": query["nonce"],
                            "sub": sub, "email": email, "verified": verified}
        return code, query["state"]


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
