"""An in-process OpenID provider that checks what a real one checks.

Behind httpx.MockTransport -- no network and no external service. It binds
each authorization code to the PKCE challenge and nonce taken from the real
authorization URL our code built, refuses a token request whose verifier
does not match the challenge, and signs ID tokens with a real RSA key
published through a real JWKS document. Shared by the SSO tests and the
fresh-provisioning test.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

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
