"""Identity: credentials and server-side sessions.

The supplied `users` table has no secret column, so an email address is an
identifier, not proof of identity. Credentials and sessions therefore live in
`app_auth`, joined to `users` by user_id, and `users` itself is never modified.

The browser holds only an opaque random token. Role, territory, region and
can_view_wac are re-read from `users` on EVERY request, so a client cannot
assert them and a scope change takes effect immediately rather than at next
login.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.auth.policy import AuthorizationError, Principal, build_principal
from app.config import get_settings
from app.db import auth_transaction

_hasher = PasswordHasher()

TOKEN_BYTES = 32


class AuthenticationError(Exception):
    """Login failed. The message is deliberately identical for every cause."""


class RateLimited(AuthenticationError):
    """Too many recent failures. Also deliberately vague about why."""


# Guessing budget. Counted per identity AND per source, because either one
# alone is trivially avoided: per-identity only lets one client spray many
# accounts, per-source only lets many clients share one target.
MAX_FAILURES_PER_EMAIL = 8
MAX_FAILURES_PER_IP = 20
FAILURE_WINDOW_MINUTES = 15


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    return hashlib.sha256(ip.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Session:
    token: str
    user_id: str
    expires_at: datetime


def authenticate(email: str, password: str, *, user_agent: str | None = None,
                 ip: str | None = None) -> tuple[Session, Principal]:
    """Verify credentials and open a session.

    Every failure path raises the same message and does comparable work, so the
    response does not reveal whether an address is registered.
    """
    settings = get_settings()
    normalized = (email or "").strip().lower()
    ip_hash = _hash_ip(ip)

    try:
        with auth_transaction() as cur:
            # Before the password is even looked at. The identical failure message
            # hides WHICH part was wrong, but on its own it does nothing to stop an
            # attacker trying again, and nothing recorded that they had.
            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE email = %s)                     AS by_email,
                  count(*) FILTER (WHERE ip_hash IS NOT NULL
                                   AND ip_hash = %s)                     AS by_ip
                FROM app_auth.login_attempts
                WHERE succeeded = false
                  AND attempted_at > now() - make_interval(mins => %s)
                """,
                (normalized, ip_hash, FAILURE_WINDOW_MINUTES),
            )
            recent = cur.fetchone()
            if (recent["by_email"] >= MAX_FAILURES_PER_EMAIL
                    or recent["by_ip"] >= MAX_FAILURES_PER_IP):
                # Deliberately NOT recorded as another failure. Counting a refusal
                # would let an attacker hold a real user out indefinitely by
                # continuing to knock; the window has to be able to expire.
                raise RateLimited(
                    "Too many sign-in attempts. Please wait a few minutes and try again."
                )

            cur.execute(
                """
                SELECT u.user_id, u.email, u.full_name, u.role, u.territory_name,
                       u.region_name, u.can_view_wac, c.password_hash, c.disabled
                FROM users u
                LEFT JOIN app_auth.credentials c ON c.user_id = u.user_id
                WHERE lower(u.email) = %s
                """,
                (normalized,),
            )
            row = cur.fetchone()

            stored = row["password_hash"] if row and row.get("password_hash") else None
            if stored is None:
                # Spend comparable time so a missing account is not faster to probe.
                _hasher.hash(password or "x")
                raise AuthenticationError("Incorrect email or password.")
            if row["disabled"]:
                raise AuthenticationError("Incorrect email or password.")

            try:
                _hasher.verify(stored, password or "")
            except (VerifyMismatchError, InvalidHashError):
                raise AuthenticationError("Incorrect email or password.") from None

            # A valid password is not enough: the account must also resolve to a
            # usable scope. An unassigned Director or RAM is refused here.
            try:
                principal = build_principal(row)
            except AuthorizationError as exc:
                raise AuthenticationError(str(exc)) from None

            token = secrets.token_urlsafe(TOKEN_BYTES)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
            cur.execute(
                "INSERT INTO app_auth.sessions "
                "(token_hash, user_id, expires_at, user_agent, ip_hash) "
                "VALUES (%s, %s, %s, %s, %s)",
                (_token_hash(token), row["user_id"], expires_at,
                 (user_agent or "")[:300] or None, ip_hash),
            )
            # A success clears the budget, so a legitimate user who mistypes twice
            # and then signs in is not held to the failures.
            _record_attempt(cur, normalized, ip_hash, succeeded=True)
            cur.execute(
                "DELETE FROM app_auth.login_attempts "
                "WHERE email = %s AND succeeded = false", (normalized,),
            )
    except RateLimited:
        raise
    except AuthenticationError:
        _record_failure(normalized, ip_hash)
        raise

    return Session(token=token, user_id=principal.user_id, expires_at=expires_at), principal


def _record_attempt(cur, email: str, ip_hash: str | None, *, succeeded: bool) -> None:
    """One row per attempt. Never the password, and the IP only as a hash."""
    cur.execute(
        "INSERT INTO app_auth.login_attempts (email, ip_hash, succeeded) "
        "VALUES (%s, %s, %s)",
        (email[:200], ip_hash, succeeded),
    )


def _record_failure(email: str, ip_hash: str | None) -> None:
    """In its OWN transaction.

    The transaction that decides a sign-in has failed then raises, and
    auth_transaction() rolls it back -- taking the record of the attempt with
    it. Recorded inline, the counter never incremented and the limit never
    engaged.
    """
    try:
        with auth_transaction() as cur:
            _record_attempt(cur, email, ip_hash, succeeded=False)
    except Exception:                          # never mask the real failure
        pass


def purge_old_login_attempts(days: int = 30) -> int:
    with auth_transaction() as cur:
        cur.execute(
            "DELETE FROM app_auth.login_attempts "
            "WHERE attempted_at < now() - make_interval(days => %s)", (days,))
        return cur.rowcount


def resolve(token: str | None) -> Principal | None:
    """Resolve a session token to a principal, or None.

    The users row is re-read here rather than cached in the session, so role,
    assignment and pricing permission are always current.
    """
    if not token:
        return None
    with auth_transaction() as cur:
        cur.execute(
            """
            SELECT u.user_id, u.email, u.full_name, u.role, u.territory_name,
                   u.region_name, u.can_view_wac
            FROM app_auth.sessions s
            JOIN users u ON u.user_id = s.user_id
            -- A disabled credential must also kill sessions already issued
            -- under it. Without this join, disabling an account left every
            -- outstanding session working until it expired on its own, which
            -- is the opposite of what disabling is for.
            JOIN app_auth.credentials c ON c.user_id = s.user_id
            WHERE s.token_hash = %s
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND c.disabled IS NOT TRUE
            """,
            (_token_hash(token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    try:
        return build_principal(row)
    except AuthorizationError:
        # A previously valid session whose account has since lost its
        # assignment resolves to nobody rather than to a default scope.
        return None


def revoke(token: str | None) -> None:
    if not token:
        return
    with auth_transaction() as cur:
        cur.execute(
            "UPDATE app_auth.sessions SET revoked_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL",
            (_token_hash(token),),
        )


def revoke_all_for_user(user_id: str) -> int:
    with auth_transaction() as cur:
        cur.execute(
            "UPDATE app_auth.sessions SET revoked_at = now() "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        return cur.rowcount


def set_credential(user_id: str, password: str, *, revoke_sessions: bool = True) -> None:
    """Create or replace a credential. Used only by the provisioning script.

    Rotating a password revokes that user's outstanding sessions by default.
    The alternative -- leaving them live -- means a rotation prompted by a
    suspected compromise does not actually end the compromised session, which
    makes the rotation close to useless. Callers that genuinely want to seed a
    credential without disturbing live sessions must say so explicitly.
    """
    with auth_transaction() as cur:
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if cur.fetchone() is None:
            raise ValueError(f"no such user_id in the supplied users table: {user_id}")
        cur.execute(
            """
            INSERT INTO app_auth.credentials (user_id, password_hash)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE
               SET password_hash = EXCLUDED.password_hash, updated_at = now()
            """,
            (user_id, hash_password(password)),
        )
        if revoke_sessions:
            cur.execute(
                "UPDATE app_auth.sessions SET revoked_at = now() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )


def set_disabled(user_id: str, disabled: bool) -> None:
    """Disable or re-enable a credential.

    Disabling revokes outstanding sessions as well as blocking new logins, so
    the effect is immediate rather than eventual. resolve() also checks the
    flag, so a session issued in the same instant is still refused.
    """
    with auth_transaction() as cur:
        cur.execute(
            "UPDATE app_auth.credentials SET disabled = %s, updated_at = now() "
            "WHERE user_id = %s",
            (disabled, user_id),
        )
        if disabled:
            cur.execute(
                "UPDATE app_auth.sessions SET revoked_at = now() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )


def purge_expired_sessions() -> int:
    with auth_transaction() as cur:
        cur.execute("DELETE FROM app_auth.sessions WHERE expires_at < now() - interval '7 days'")
        return cur.rowcount
