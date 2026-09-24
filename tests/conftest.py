"""Shared test fixtures.

Integration and security tests run against a loaded PostgreSQL database. They
skip (rather than fail) when no published dataset is present, so a clean
checkout can run the unit suite before loading 2M rows.
"""

from __future__ import annotations

import pathlib
import secrets

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _database_available() -> tuple[bool, str]:
    try:
        from app.db import owner_transaction

        with owner_transaction() as cur:
            cur.execute(
                "SELECT dataset_id, load_mode FROM app_meta.dataset_manifest "
                "WHERE load_state = 'published' ORDER BY published_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row is None:
            return False, "no published dataset (run scripts/load_data.py)"
        return True, row["load_mode"]
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, f"database unavailable: {type(exc).__name__}"


DB_OK, DB_REASON = _database_available()

needs_db = pytest.mark.skipif(not DB_OK, reason=f"needs a loaded database: {DB_REASON}")
needs_full = pytest.mark.skipif(
    DB_REASON != "full", reason="needs the full dataset (scripts/load_data.py --mode full)"
)


@pytest.fixture(scope="session")
def anchor() -> dict:
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute(
            "SELECT reporting_anchor FROM app_meta.dataset_manifest "
            "WHERE load_state = 'published' ORDER BY published_at DESC LIMIT 1"
        )
        return cur.fetchone()["reporting_anchor"]


@pytest.fixture(scope="session")
def test_users() -> dict:
    """Provision throwaway credentials for one user of each role.

    Passwords are generated per run and never written to disk, so the suite
    does not depend on evaluator_logins.json existing.
    """
    from app.auth.identity import set_credential
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (u.role)
                   u.user_id, u.email, u.role, u.territory_name, u.region_name
            FROM users u
            ORDER BY u.role,
                     CASE
                       WHEN u.role = 'exec' THEN 1
                       WHEN u.role = 'director' AND EXISTS (
                            SELECT 1 FROM zip_territory z
                            WHERE z.region_name = u.region_name) THEN 1
                       WHEN u.role = 'ram' AND EXISTS (
                            SELECT 1 FROM zip_territory z
                            WHERE z.territory_name = u.territory_name) THEN 1
                       ELSE 2
                     END,
                     u.user_id
            """
        )
        rows = cur.fetchall()

    out = {}
    for row in rows:
        password = secrets.token_urlsafe(16)
        set_credential(row["user_id"], password)
        out[row["role"]] = {**dict(row), "password": password}
    return out


@pytest.fixture(scope="session")
def principals(test_users):
    from app.auth.identity import authenticate

    return {
        role: authenticate(item["email"], item["password"])[1]
        for role, item in test_users.items()
    }


@pytest.fixture(scope="session")
def exec_user(principals):
    return principals["exec"]


@pytest.fixture(scope="session")
def director_user(principals):
    return principals["director"]


@pytest.fixture(scope="session")
def ram_user(principals):
    return principals["ram"]


@pytest.fixture(scope="session")
def pipeline():
    from app.llm.planner import OfflinePlanner
    from app.pipeline import Pipeline

    # The deterministic planner, so a failing test means the pipeline broke
    # rather than that a model's wording drifted. Live-model accuracy is
    # measured separately in evals/.
    return Pipeline(OfflinePlanner())


@pytest.fixture(scope="session")
def compiler():
    from app.analytics.compiler import Compiler

    return Compiler()


def reference_scalar(sql: str, params: tuple, *, scope_kind: str, scope_value: str | None,
                     wac: bool = False):
    """Run INDEPENDENTLY WRITTEN reference SQL through the same security boundary.

    The point is that the expected value never comes from the compiler under
    test. These statements are written by hand in the test files.
    """
    from app.db import analytics_transaction

    with analytics_transaction(
        scope_kind=scope_kind, scope_value=scope_value, wac_authorized=wac
    ) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else list(row.values())[0]


def run_plan(compiler, plan, anchor, principal):
    """Compile, validate and execute a plan as a principal; return rows."""
    from app.analytics.validator import validate
    from app.db import analytics_transaction

    query = compiler.compile(plan, anchor=anchor)
    validate(query.sql, wac_authorized=principal.wac_authorized)
    with analytics_transaction(
        scope_kind=principal.scope_kind,
        scope_value=principal.scope_value,
        wac_authorized=principal.wac_authorized,
    ) as cur:
        cur.execute(query.sql, query.params)
        return cur.fetchall(), query


@pytest.fixture(scope="session", autouse=True)
def _close_pools_at_end():
    yield
    from app.db import close_pools

    close_pools()
