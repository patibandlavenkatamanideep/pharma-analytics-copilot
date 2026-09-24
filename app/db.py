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
    """Assert at startup that the roles we actually connect as cannot bypass the
    controls above.

    Returns a list of problems; an empty list means the boundary holds.

    Two things this deliberately does NOT do any more:

      * It does not test a hardcoded list of role names. The names are read
        from the configuration, so the roles inspected are the ones the
        application will really log in as. Checking `pac_rt_scoped` while the
        process connects as something else proves nothing.

      * It does not read role attributes only. SUPERUSER and BYPASSRLS are
        reachable through role MEMBERSHIP -- a role with neither attribute set
        can `SET ROLE` to one that has them -- so membership is resolved
        transitively with pg_has_role(). Likewise column and table access is
        asked via has_*_privilege(), which accounts for privileges inherited
        from granted roles rather than only those granted directly.

    A role named in the configuration but missing from the database is itself a
    problem. The previous version filtered pg_roles by name, so a missing role
    simply produced no rows and the check passed.
    """
    settings = get_settings()
    analytics_roles = {
        "exec": settings.db_exec_user,
        "scoped": settings.db_scoped_user,
    }
    connecting = {**analytics_roles, "auth": settings.db_auth_user}
    problems: list[str] = []

    # Over the auth connection, not the owner one: everything consulted below
    # lives in pg_catalog and is readable by any role, so the serving process
    # never needs owner credentials. Ingestion still does, and keeps them.
    with auth_transaction() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(connecting.values()),),
        )
        present = {r["rolname"] for r in cur.fetchall()}
        for purpose, name in connecting.items():
            if name not in present:
                problems.append(f"configured {purpose} role {name!r} does not exist")

        usable = [n for n in connecting.values() if n in present]
        if not usable:
            return problems

        # SUPERUSER / BYPASSRLS, including where they are reachable by being a
        # member of some other role that holds them.
        cur.execute(
            """
            SELECT m.rolname AS member, r.rolname AS held, r.rolsuper, r.rolbypassrls
            FROM pg_roles m
            JOIN pg_roles r ON pg_has_role(m.oid, r.oid, 'MEMBER')
            WHERE m.rolname = ANY(%s) AND (r.rolsuper OR r.rolbypassrls)
            """,
            (usable,),
        )
        for row in cur.fetchall():
            attrs = ", ".join(
                a for a, on in (("SUPERUSER", row["rolsuper"]),
                                ("BYPASSRLS", row["rolbypassrls"])) if on
            )
            via = "" if row["held"] == row["member"] else f" via membership in {row['held']}"
            problems.append(f"{row['member']} has {attrs}{via}")

        for table in ("organizations", "sales"):
            cur.execute(
                "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                "       pg_get_userbyid(c.relowner) AS owner, "
                "       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = %s AND c.relkind = 'r' AND n.nspname = 'public'",
                (table,),
            )
            row = cur.fetchone()
            if row is None:
                problems.append(f"protected table {table} is missing")
                continue
            if not row["relrowsecurity"]:
                problems.append(f"RLS not enabled on {table}")
            if row["policies"] == 0:
                # Enabled with no policy denies everything, which is safe but
                # means the deployment is broken rather than protected.
                problems.append(f"RLS enabled on {table} but no policy is defined")
            # An owner bypasses its own table's RLS unless FORCE is set, so an
            # analytics role must never own a protected table.
            for purpose, name in analytics_roles.items():
                if name in present and row["owner"] == name and not row["relforcerowsecurity"]:
                    problems.append(
                        f"{purpose} role {name} owns {table} without FORCE ROW LEVEL SECURITY"
                    )

        scoped = settings.db_scoped_user
        if scoped in present:
            # Effective, not direct: has_column_privilege resolves inheritance.
            cur.execute(
                "SELECT has_column_privilege(%s, 'sales', 'wac', 'SELECT') AS can_wac",
                (scoped,),
            )
            if cur.fetchone()["can_wac"]:
                problems.append(f"{scoped} can SELECT sales.wac")

            cur.execute(
                "SELECT has_table_privilege(%s, 'users', 'SELECT') AS can_users", (scoped,))
            if cur.fetchone()["can_users"]:
                problems.append(f"{scoped} can read the users table")

            # The identity and conversation schemas are not the analytics
            # role's business. (app_meta is not listed here because the scoped
            # role has no USAGE on it either -- the manifest is read over the
            # auth connection.)
            for schema in ("app_auth", "app_conv"):
                cur.execute(
                    "SELECT has_schema_privilege(%s, %s, 'USAGE') AS u", (scoped, schema))
                if cur.fetchone()["u"]:
                    problems.append(f"{scoped} has USAGE on {schema}")

        # The exec role is the only one that may price. If it cannot, execs get
        # errors instead of answers, so the deployment is misprovisioned too.
        exec_role = settings.db_exec_user
        if exec_role in present:
            cur.execute(
                "SELECT has_column_privilege(%s, 'sales', 'wac', 'SELECT') AS can_wac",
                (exec_role,),
            )
            if not cur.fetchone()["can_wac"]:
                problems.append(f"{exec_role} cannot SELECT sales.wac")

    return problems
