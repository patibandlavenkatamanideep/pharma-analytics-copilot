"""Database access.

Two independent controls, deliberately kept orthogonal:

  * WHICH CONNECTION POOL is used decides COLUMN access. The Exec pool logs in as
    a role holding SELECT on sales.wac; the scoped pool logs in as a role that
    was never granted it. Pricing access is therefore a property of the TCP
    connection, not of a branch in application code.

  * The transaction-local GUCs app.scope_kind / app.scope_value decide ROW
    access, through the RLS policies in migrations/004_security.sql.

Both are bound by trusted server code inside one read-only transaction and are
discarded at COMMIT, so a pooled connection cannot carry one user's scope into
another user's request.
"""

from __future__ import annotations

import atexit
import logging
from contextlib import contextmanager
from typing import Iterator, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

_POOLS: dict[str, ConnectionPool] = {}


def get_pool(role: Literal["owner", "auth", "exec", "scoped"]) -> ConnectionPool:
    settings = get_settings()
    if role not in _POOLS:
        _POOLS[role] = ConnectionPool(
            settings.dsn(role),
            min_size=1,
            max_size=8 if role in ("exec", "scoped") else 4,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _POOLS[role]


def close_pools() -> None:
    for pool in _POOLS.values():
        try:
            pool.close()
        except Exception:                      # pragma: no cover - best effort
            log.warning("pool close failed", exc_info=True)
    _POOLS.clear()


# psycopg_pool's worker threads are not daemons, so an exception escaping a
# script leaves the interpreter waiting on them and the process appears to hang
# for ~20s before printing "couldn't stop thread". Registering the teardown at
# exit means pools are released on every path, not just the happy one.
atexit.register(close_pools)


class ScopeBindingError(RuntimeError):
    """Raised when a scope could not be bound. Always fails the request closed."""


@contextmanager
def analytics_transaction(
    *,
    scope_kind: Literal["global", "region", "territory"],
    scope_value: str | None,
    wac_authorized: bool,
    settings: Settings | None = None,
) -> Iterator[psycopg.Cursor]:
    """A read-only, time-bounded transaction with RLS scope bound.

    scope_kind is never taken from the browser or from the language model; it is
    derived server-side from the supplied users table.
    """
    settings = settings or get_settings()

    if scope_kind not in ("global", "region", "territory"):
        raise ScopeBindingError(f"unknown scope kind {scope_kind!r}")
    if scope_kind != "global" and not scope_value:
        # A Director or RAM with no assignment has no provable scope. Deny
        # rather than fall back to global.
        raise ScopeBindingError(f"scope kind {scope_kind!r} requires an assignment")

    pool = get_pool("exec" if wac_authorized else "scoped")

    with pool.connection() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                # Read-only is declared with an explicit statement rather than
                # psycopg's connection attribute: the attribute is applied when
                # the pool hands the connection back, which interacts badly with
                # a transaction that ended in an error. A statement inside the
                # transaction is scoped to exactly this transaction.
                cur.execute("SET TRANSACTION READ ONLY")
                # set_config(..., is_local => true) rather than SET LOCAL: it is
                # the parameterized form, so no value is ever interpolated into
                # SQL text, and it is equally transaction-scoped -- every setting
                # below is discarded at COMMIT or ROLLBACK.
                cur.execute(
                    """
                    SELECT
                        -- Fixed, minimal search path: an unqualified identifier
                        -- can never resolve into an attacker-created schema.
                        set_config('search_path', 'public, app_ref', true),
                        set_config('statement_timeout', %s, true),
                        set_config('idle_in_transaction_session_timeout', %s, true),
                        -- Row scope for the RLS policies.
                        set_config('app.scope_kind', %s, true),
                        set_config('app.scope_value', %s, true)
                    """,
                    (
                        str(settings.statement_timeout_ms),
                        "10000",
                        scope_kind,
                        scope_value or "",
                    ),
                )
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def auth_transaction() -> Iterator[psycopg.Cursor]:
    """Identity/session/conversation access. Never used for analytical SQL."""
    with get_pool("auth").connection() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def owner_transaction(autocommit: bool = False) -> Iterator[psycopg.Cursor]:
    """Migrations and ingestion only."""
    with get_pool("owner").connection() as conn:
        conn.autocommit = autocommit
        try:
            with conn.cursor() as cur:
                yield cur
            if not autocommit:
                conn.commit()
        except Exception:
            if not autocommit:
                conn.rollback()
            raise


def verify_runtime_role_safety() -> list[str]:
    """Assert at startup that the runtime roles cannot bypass the controls above.

    Returns a list of problems; an empty list means the boundary holds. This is
    checked on every boot so a mis-provisioned database fails loudly instead of
    silently serving unrestricted data.
    """
    problems: list[str] = []
    with owner_transaction() as cur:
        cur.execute(
            """
            SELECT rolname, rolsuper, rolbypassrls
            FROM pg_roles
            WHERE rolname IN ('pac_rt_exec','pac_rt_scoped','pac_exec_login','pac_scoped_login')
            """
        )
        for row in cur.fetchall():
            if row["rolsuper"]:
                problems.append(f"{row['rolname']} is SUPERUSER")
            if row["rolbypassrls"]:
                problems.append(f"{row['rolname']} has BYPASSRLS")

        # RLS must be enabled on both protected tables.
        cur.execute(
            "SELECT relname, relrowsecurity FROM pg_class "
            "WHERE relname IN ('organizations','sales') AND relkind = 'r'"
        )
        for row in cur.fetchall():
            if not row["relrowsecurity"]:
                problems.append(f"RLS not enabled on {row['relname']}")

        # The scoped role must hold no privilege whatsoever on sales.wac, and
        # must not hold a table-wide grant that would supersede the column list.
        cur.execute(
            "SELECT has_column_privilege('pac_rt_scoped', 'sales', 'wac', 'SELECT') AS can_wac"
        )
        if cur.fetchone()["can_wac"]:
            problems.append("pac_rt_scoped can SELECT sales.wac")

        cur.execute("SELECT has_table_privilege('pac_rt_scoped', 'users', 'SELECT') AS can_users")
        if cur.fetchone()["can_users"]:
            problems.append("pac_rt_scoped can read the users table")

        for schema in ("app_auth", "app_conv", "app_meta"):
            cur.execute(
                "SELECT has_schema_privilege('pac_rt_scoped', %s, 'USAGE') AS u", (schema,)
            )
            if cur.fetchone()["u"] and schema != "app_meta":
                problems.append(f"pac_rt_scoped has USAGE on {schema}")

    return problems
