"""Fixtures for the security suite.

Everything here runs against a DISPOSABLE database
(`pharma_analytics_authtest`, built by `scripts/build_authtest_db.py`) because
these tests change a user's role, territory and pricing permission to simulate
a real permission change. The working database is never dropped, truncated or
edited by the suite.

The identities are created per test and deleted afterwards. No evaluator
credential is read, rotated or used.
"""

from __future__ import annotations

import os
import secrets

import pytest

from tests.security.helpers import Identity

AUTHTEST_DB = os.environ.get("PAC_AUTHTEST_DB", "pharma_analytics_authtest")


# ---------------------------------------------------------------------------
# Disposable database
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def authtest_db():
    """Point settings and pools at the disposable database for this module."""
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools

    if AUTHTEST_DB == os.environ.get("PAC_DB_NAME") or AUTHTEST_DB == "pharma_analytics":
        pytest.fail("refusing to run mutating auth tests against the working database")

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = AUTHTEST_DB
    get_settings.cache_clear()
    close_pools()
    clear_caches()

    try:
        from app.db import owner_transaction

        with owner_transaction() as cur:
            cur.execute("SELECT count(*) AS n FROM sales")
            if cur.fetchone()["n"] == 0:
                pytest.skip("authtest database is empty; run scripts/build_authtest_db.py")
        yield
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        pytest.skip(f"authtest database unavailable: {type(exc).__name__}: {exc}")
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


@pytest.fixture(scope="module")
def real_scopes(authtest_db):
    """Two territories in different regions, so a move is a genuine change."""
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute(
            "SELECT DISTINCT ON (region_name) territory_name, region_name "
            "FROM zip_territory ORDER BY region_name, territory_name"
        )
        rows = cur.fetchall()
    if len(rows) < 2:
        pytest.skip("need at least two regions in zip_territory")
    return [dict(r) for r in rows[:2]]


# ---------------------------------------------------------------------------
# Isolated test identities
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def make_identity(authtest_db):
    """Create disposable users; remove them and everything they own at the end."""
    from app.auth.identity import set_credential
    from app.db import owner_transaction

    created: list[str] = []

    def _make(role: str, *, territory=None, region=None, can_view_wac=0) -> Identity:
        suffix = secrets.token_hex(4)
        user_id = f"pactest-{role}-{suffix}"
        email = f"pactest-{role}-{suffix}@test.invalid"
        password = secrets.token_urlsafe(18)
        with owner_transaction() as cur:
            cur.execute(
                "INSERT INTO users (user_id, email, full_name, role, "
                "territory_name, region_name, can_view_wac) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (user_id, email, f"Test {role.title()} {suffix}", role,
                 territory, region, can_view_wac),
            )
        created.append(user_id)
        set_credential(user_id, password)
        return Identity(user_id, email, password)

    yield _make

    with owner_transaction() as cur:
        for user_id in created:
            cur.execute(
                "DELETE FROM app_conv.turns WHERE conversation_id IN "
                "(SELECT conversation_id FROM app_conv.conversations "
                " WHERE owner_user_id = %s)",
                (user_id,),
            )
            cur.execute(
                "DELETE FROM app_conv.conversations WHERE owner_user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_auth.sessions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_auth.credentials WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))


# ---------------------------------------------------------------------------
# HTTP client with real sessions
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client(authtest_db):
    """A real HTTP client over the real app, with the real cookie jar."""
    from fastapi.testclient import TestClient

    os.environ["PAC_COOKIE_SECURE"] = "false"
    os.environ.setdefault("PAC_LLM_PROVIDER", "offline")
    from app.config import get_settings

    get_settings.cache_clear()

    from app.api.main import app

    with TestClient(app) as c:
        yield c
