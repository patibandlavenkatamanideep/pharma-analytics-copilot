"""Shared test fixtures.

Integration and security tests run against a loaded PostgreSQL database. They
skip (rather than fail) when no published dataset is present, so a clean
checkout can run the unit suite before loading 2M rows.
"""

from __future__ import annotations

import pathlib

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
    """One usable user per role, from the supplied users table.

    NO CREDENTIAL IS CREATED OR ROTATED HERE.

    This fixture used to mint a password per role and call set_credential()
    against whatever database was active -- which, for a plain `pytest tests`,
    is the WORKING one. Every run silently rotated the real evaluator logins,
    so credentials that had been issued to a reviewer stopped working and
    nothing said why. The same defect was fixed in make_demo.py, run_evals.py
    and benchmark.py and was missed here.

    Tests that need a principal do not need a password: principal_for_user_id()
    builds one from the users table directly. Tests that genuinely need to sign
    in over HTTP create their own disposable identities against a disposable
    database -- see tests/security/conftest.py.
    """
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
        return {row["role"]: dict(row) for row in cur.fetchall()}


@pytest.fixture(scope="session")
def principals(test_users):
    """Built from the users table, so no password is involved."""
    from app.auth.policy import principal_for_user_id

    return {
        role: principal_for_user_id(row["user_id"])
        for role, row in test_users.items()
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


# The --release-gate plugin lives in its own module so it can be loaded
# independently; see tests/release_gate.py for why the gate exists.
pytest_plugins = ["tests.release_gate"]
