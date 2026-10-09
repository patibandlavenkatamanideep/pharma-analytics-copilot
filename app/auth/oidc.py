"""Sign-in through an OpenID Connect provider.

Configurable and off by default (PAC_OIDC_ENABLED). Local password sign-in is
unchanged and remains the evaluator workflow. Cryptography and protocol
details come from maintained libraries -- Authlib for the authorization-code
exchange with PKCE, joserfc for ID-token signatures and claims -- not from
code written here.

What is validated, and where:

* **state** -- random, single use, stored server-side by hash, expires in
  ten minutes. A callback with an unknown, reused or expired state is
  refused: it is either forged or replayed.
* **browser binding** -- the attempt belongs to the browser that started
  it. Start sets an HttpOnly cookie holding a 256-bit secret, and only its
  hash is stored with the attempt. The callback must present that cookie, or
  it is refused BEFORE the code is exchanged. Without this, state proves only
  that the attempt exists, not whose browser it is in, so a callback
  obtained in one browser signed in any other (login CSRF; review of
  1 October 2026, R1). The cookie is `__Host-` prefixed whenever cookies are
  Secure, so a sibling subdomain cannot plant one. Tabs: a start in a browser
  that already has an attempt pending shares that attempt's secret, so
  sign-ins begun in two tabs can both finish.
* **PKCE** -- an S256 code challenge on the authorization request, the
  verifier on the token request. An intercepted code is useless without it.
* **ID token** -- signature against the issuer's published keys (re-fetched
  once on an unknown key id, which is how providers rotate keys), algorithm
  from an allowlist (never "none", never HMAC with a public key), issuer
  exactly as configured, audience containing this client (and azp when
  there are several audiences), expiry and issued-at with a small leeway,
  and the nonce sent with this sign-in.
* **identity** -- the verified (issuer, subject) pair, looked up in
  app_auth.identities. Email is not the key. An identity with no link is
  refused unless linking by provider-verified email is explicitly enabled.
* **redirect** -- only a same-site relative path is honoured after sign-in.

The session that results is the same opaque server-side session as a
password sign-in: idle expiry, rotation and account disabling apply as-is.
Enterprise MFA is the provider's policy, not this code's.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx2 as httpx  # the client Authlib 1.8 is built on; legacy httpx is deprecated there

from app.auth.identity import AuthenticationError, issue_session
from app.auth.policy import AuthorizationError, Principal, build_principal
from app.config import get_settings
from app.db import auth_transaction

PENDING_TTL_SECONDS = 600
CLOCK_LEEWAY_SECONDS = 60
_METADATA_TTL_SECONDS = 3600

#: The cookie binding a sign-in attempt to the browser that started it.
BINDING_COOKIE = "pac_oidc"
_BINDING_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")      # secrets.token_urlsafe(32)


def binding_cookie_name(secure: bool) -> str:
    """`__Host-pac_oidc` over HTTPS: the browser accepts it only with Secure,
    Path=/ and no Domain, so no other host under the same parent domain can
    set it. Plain `pac_oidc` only where cookies are not Secure (local HTTP)."""
    return f"__Host-{BINDING_COOKIE}" if secure else BINDING_COOKIE


class OIDCError(AuthenticationError):
    """A sign-in through the provider failed. `code` is safe to show."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Begun:
    authorization_url: str
    state: str
    #: The browser secret to set as the binding cookie. Never stored.
    binding: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_redirect(target: str | None) -> str:
    """A same-site relative path, or "/". "//evil.example" is a
    protocol-relative URL, not a path, and is refused."""
    if not target or not target.startswith("/") or target.startswith("//") \
            or "\\\\" in target or "://" in target:
        return "/"
    return target


class Provider:
    """One configured issuer. `http` is injectable so tests can stand up a
    provider without a network; production uses a real client with timeouts."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self._transport = transport
        self._http = lambda: httpx.Client(timeout=10.0, transport=transport)
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._keys = None

    @property
    def settings(self):
        # Read per call, not captured at construction: a provider object
        # lives for the process, and configuration can be reloaded.
        return get_settings()

    # -- discovery ---------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        if self._metadata is None or time.time() - self._metadata_at > _METADATA_TTL_SECONDS:
            issuer = self.settings.oidc_issuer.rstrip("/")
            with self._http() as client:
                r = client.get(f"{issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                meta = r.json()
            # The document must describe the issuer we were configured with;
            # anything else is a different provider answering for it.
            if meta.get("issuer", "").rstrip("/") != issuer:
                raise OIDCError("issuer_mismatch", "The identity provider is misconfigured.")
            self._metadata, self._metadata_at, self._keys = meta, time.time(), None
        return self._metadata

    def keys(self, *, refresh: bool = False):
        from joserfc.jwk import KeySet

        if self._keys is None or refresh:
            with self._http() as client:
                r = client.get(self.metadata()["jwks_uri"])
                r.raise_for_status()
                self._keys = KeySet.import_key_set(r.json())
        return self._keys

    # -- the flow ----------------------------------------------------------------

    def begin(self, redirect_after: str | None = None, *,
              browser: str | None = None) -> Begun:
        """Start an attempt bound to this browser. `browser` is the binding
        cookie it already holds, if any: reused while another attempt from it
        is still pending (a second tab), otherwise replaced."""
        from authlib.integrations.httpx_client import OAuth2Client

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        with auth_transaction() as cur:
            cur.execute("DELETE FROM app_auth.oidc_pending WHERE expires_at <= now()")
            binding = None
            if browser and _BINDING_SHAPE.fullmatch(browser):
                cur.execute("SELECT 1 FROM app_auth.oidc_pending WHERE binding_hash = %s "
                            "LIMIT 1", (_hash(browser),))
                if cur.fetchone():
                    binding = browser
            binding = binding or secrets.token_urlsafe(32)
            cur.execute(
                "INSERT INTO app_auth.oidc_pending (state_hash, binding_hash, nonce, "
                "  code_verifier, redirect_after, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
                (_hash(state), _hash(binding), nonce, verifier, safe_redirect(redirect_after),
                 PENDING_TTL_SECONDS))
        client = OAuth2Client(
            client_id=self.settings.oidc_client_id,
            redirect_uri=self.settings.oidc_redirect_uri,
            scope=self.settings.oidc_scopes,
            code_challenge_method="S256",
        )
        url, _ = client.create_authorization_url(
            self.metadata()["authorization_endpoint"], state=state,
            code_verifier=verifier, nonce=nonce)
        return Begun(authorization_url=url, state=state, binding=binding)

    def cancel(self, *, state: str, browser: str | None) -> bool:
        """The provider reported the attempt declined or cancelled. Ends it,
        but only from the browser that started it: another browser cannot
        cancel someone else's sign-in."""
        with auth_transaction() as cur:
            cur.execute("DELETE FROM app_auth.oidc_pending "
                        "WHERE state_hash = %s AND binding_hash = %s",
                        (_hash(state or ""), _hash(browser or "")))
            return cur.rowcount > 0

    def pending_in(self, browser: str | None) -> bool:
        """Whether this browser has another attempt still pending."""
        if not browser:
            return False
        with auth_transaction() as cur:
            cur.execute("SELECT 1 FROM app_auth.oidc_pending WHERE binding_hash = %s "
                        "AND expires_at > now() LIMIT 1", (_hash(browser),))
            return cur.fetchone() is not None

    def complete(self, *, code: str, state: str, browser: str | None,
                 user_agent: str | None = None,
                 ip_hash: str | None = None) -> tuple[str, datetime, Principal, str]:
        """Finish a sign-in. `browser` is the binding cookie this callback
        arrived with. Returns (session token, expiry, principal, where to
        send the user)."""
        from authlib.integrations.httpx_client import OAuth2Client

        # Single use, and only by the browser that started it: the row is
        # deleted as it is read, and only when the binding matches, so a
        # replayed callback finds nothing and a callback carried to another
        # browser neither succeeds nor uses up the real one.
        other = None
        with auth_transaction() as cur:
            cur.execute(
                "DELETE FROM app_auth.oidc_pending WHERE state_hash = %s AND binding_hash = %s "
                "RETURNING nonce, code_verifier, redirect_after, expires_at > now() AS live",
                (_hash(state or ""), _hash(browser or "")))
            pending = cur.fetchone()
            if pending is None:
                cur.execute("SELECT expires_at > now() AS live FROM app_auth.oidc_pending "
                            "WHERE state_hash = %s", (_hash(state or ""),))
                other = cur.fetchone()
        if pending is None and other is not None and other["live"]:
            # Checked before the code is exchanged: nothing is redeemed.
            raise OIDCError("browser_mismatch",
                            "This sign-in was started in a different browser. "
                            "Start again from this one.")
        if pending is None or not pending["live"]:
            raise OIDCError("invalid_state",
                            "That sign-in link has expired or was already used. Try again.")

        # Authlib performs the exchange: grant type, redirect URI, client
        # authentication and the PKCE verifier.
        client = OAuth2Client(
            client_id=self.settings.oidc_client_id,
            client_secret=self.settings.oidc_client_secret or None,
            redirect_uri=self.settings.oidc_redirect_uri,
            token_endpoint_auth_method=("client_secret_post" if self.settings.oidc_client_secret
                                        else "none"),
            timeout=10.0, transport=self._transport,
        )
        try:
            with client:
                tokens = client.fetch_token(self.metadata()["token_endpoint"], code=code,
                                            code_verifier=pending["code_verifier"])
        except httpx.HTTPError:
            raise OIDCError("provider_unavailable",
                            "The identity provider could not be reached.") from None
        except Exception:
            # Authlib raises OAuthError for a refused grant (a wrong verifier,
            # a used or expired code); the detail is the provider's, not ours
            # to repeat to the user.
            raise OIDCError("token_rejected", "The identity provider refused the sign-in.") from None
        id_token = tokens.get("id_token")
        if not id_token:
            raise OIDCError("invalid_token", "The identity provider returned no ID token.")

        claims = self._verify(id_token, nonce=pending["nonce"])
        principal = self._link(claims)
        with auth_transaction() as cur:
            token, expires_at = issue_session(cur, principal.user_id,
                                              user_agent=user_agent, ip_hash=ip_hash)
        return token, expires_at, principal, pending["redirect_after"]

    # -- validation ----------------------------------------------------------------

    def _verify(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        from joserfc import jwt
        from joserfc.errors import InvalidKeyIdError, JoseError

        algorithms = [a.strip() for a in self.settings.oidc_algorithms.split(",") if a.strip()]
        try:
            try:
                token = jwt.decode(id_token, self.keys(), algorithms=algorithms)
            except InvalidKeyIdError:
                # A key id we have not seen: the provider has rotated keys.
                token = jwt.decode(id_token, self.keys(refresh=True), algorithms=algorithms)
        except JoseError:
            raise OIDCError("invalid_token", "The sign-in token could not be verified.") from None

        issuer = self.settings.oidc_issuer.rstrip("/")
        registry = jwt.JWTClaimsRegistry(
            leeway=CLOCK_LEEWAY_SECONDS,
            iss={"essential": True, "value": issuer},
            sub={"essential": True},
            aud={"essential": True, "value": self.settings.oidc_client_id},
            exp={"essential": True},
            iat={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        try:
            registry.validate(token.claims)
        except JoseError:
            raise OIDCError("invalid_token", "The sign-in token could not be verified.") from None
        aud = token.claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 \
                and token.claims.get("azp") != self.settings.oidc_client_id:
            raise OIDCError("invalid_token", "The sign-in token was issued to another client.")
        return token.claims

    def _link(self, claims: dict[str, Any]) -> Principal:
        issuer, subject = claims["iss"].rstrip("/"), str(claims["sub"])
        with auth_transaction() as cur:
            cur.execute(
                "SELECT u.user_id, u.email, u.full_name, u.role, u.territory_name, "
                "       u.region_name, u.can_view_wac, c.disabled "
                "FROM app_auth.identities i JOIN users u ON u.user_id = i.user_id "
                "LEFT JOIN app_auth.credentials c ON c.user_id = u.user_id "
                "WHERE i.issuer = %s AND i.subject = %s", (issuer, subject))
            row = cur.fetchone()
            if row is None and self.settings.oidc_link_by_verified_email \
                    and claims.get("email_verified") is True and claims.get("email"):
                # Opt-in, and only from an email the PROVIDER verified. After
                # this the (issuer, subject) pair is the key; a later change of
                # address at the provider does not move the link.
                cur.execute(
                    "SELECT u.user_id, u.email, u.full_name, u.role, u.territory_name, "
                    "       u.region_name, u.can_view_wac, c.disabled "
                    "FROM users u LEFT JOIN app_auth.credentials c ON c.user_id = u.user_id "
                    "WHERE lower(u.email) = lower(%s)", (claims["email"],))
                row = cur.fetchone()
                if row is not None:
                    cur.execute("INSERT INTO app_auth.identities (issuer, subject, user_id) "
                                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                (issuer, subject, row["user_id"]))
            if row is None:
                raise OIDCError("not_linked",
                                "This sign-in is not linked to an account here. Ask an "
                                "administrator to link it.")
            if row["disabled"]:
                raise OIDCError("disabled", "This account is disabled.")
            # Sessions are resolved through app_auth.credentials, which is how
            # disabling an account ends its sessions. An SSO-only user gets a
            # row whose hash can never verify a password.
            cur.execute("INSERT INTO app_auth.credentials (user_id, password_hash) "
                        "VALUES (%s, '!') ON CONFLICT (user_id) DO NOTHING", (row["user_id"],))
            cur.execute("UPDATE app_auth.identities SET last_login_at = now() "
                        "WHERE issuer = %s AND subject = %s", (issuer, subject))
        try:
            return build_principal(row)
        except AuthorizationError as exc:
            raise OIDCError("no_scope", str(exc)) from None
