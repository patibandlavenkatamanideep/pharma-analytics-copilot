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

    with auth_transaction() as cur:
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
             (user_agent or "")[:300] or None, _hash_ip(ip)),
        )

    return Session(token=token, user_id=principal.user_id, expires_at=expires_at), principal


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
            WHERE s.token_hash = %s
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
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


def set_credential(user_id: str, password: str) -> None:
    """Create or replace a credential. Used only by the provisioning script."""
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


def purge_expired_sessions() -> int:
    with auth_transaction() as cur:
        cur.execute("DELETE FROM app_auth.sessions WHERE expires_at < now() - interval '7 days'")
        return cur.rowcount
