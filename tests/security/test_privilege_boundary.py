"""The startup boundary check must inspect effective grants, not role names.

Runs against the disposable database. The roles it creates are named
`pactest_*` and are dropped again; the real runtime roles are never altered,
which matters because PostgreSQL roles are cluster-wide and altering
`pac_scoped_login` would change the working database too.

The defect these cover: the check listed four hardcoded role names, read only
their own `rolsuper`/`rolbypassrls` attributes, and filtered `pg_roles` by
name. So it was blind to three separate things -- a deployment configured to
connect as some other role, a role that reaches BYPASSRLS by being a member of
a role that has it, and a configured role that does not exist at all (no row,
therefore no problem reported).
"""

from __future__ import annotations

import os
import secrets

import pytest


ADMIN_DSN = os.environ.get("PAC_ADMIN_DSN", "postgresql:///postgres")


@pytest.fixture
def admin_cursor(authtest_db):
    """A connection able to create roles.

    The owner role deliberately lacks CREATEROLE, which is why this is separate
    and why the test skips rather than quietly passing when no administrative
    connection is available.
    """
    import psycopg

    try:
        conn = psycopg.connect(ADMIN_DSN, autocommit=True)
    except Exception as exc:
        pytest.skip(f"no administrative connection ({ADMIN_DSN}): {type(exc).__name__}")
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user")
            row = cur.fetchone()
            if not row or not row[0]:
                pytest.skip("administrative connection cannot create roles")
            yield cur


@pytest.fixture
def escalation_roles(admin_cursor):
    """A throwaway login role and a throwaway role holding BYPASSRLS."""
    tag = secrets.token_hex(4)
    login = f"pactest_login_{tag}"
    dangerous = f"pactest_bypass_{tag}"

    admin_cursor.execute(f'CREATE ROLE "{login}" LOGIN')
    admin_cursor.execute(f'CREATE ROLE "{dangerous}" BYPASSRLS')
    try:
        yield login, dangerous
    finally:
        admin_cursor.execute(f'REVOKE "{dangerous}" FROM "{login}"')
        for role in (login, dangerous):
            admin_cursor.execute(f'DROP ROLE IF EXISTS "{role}"')


def _problems_with(**env) -> list[str]:
    """Run the boundary check with specific roles configured."""
    from app.config import get_settings
    from app.db import verify_runtime_role_safety

    previous = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    get_settings.cache_clear()
    try:
        return verify_runtime_role_safety()
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def test_bypassrls_reached_through_membership_is_detected(escalation_roles, admin_cursor):
    login, dangerous = escalation_roles

    # Before the grant the role is unremarkable, so anything reported below is
    # attributable to the membership and not to the role merely being new.
    baseline = _problems_with(PAC_DB_SCOPED_USER=login)
    assert not any("BYPASSRLS" in p for p in baseline)

    admin_cursor.execute(f'GRANT "{dangerous}" TO "{login}"')

    after = _problems_with(PAC_DB_SCOPED_USER=login)
    assert any("BYPASSRLS" in p and login in p for p in after), (
        f"membership in a BYPASSRLS role was not detected: {after}"
    )
    assert any(dangerous in p for p in after), "the report does not name the path"


def test_a_configured_role_that_does_not_exist_is_a_problem():
    missing = f"pactest_absent_{secrets.token_hex(4)}"
    problems = _problems_with(PAC_DB_SCOPED_USER=missing)
    assert any("does not exist" in p and missing in p for p in problems), (
        f"a missing role was silently accepted: {problems}"
    )


def test_the_check_follows_configuration_rather_than_default_names(
    escalation_roles, admin_cursor
):
    """A deployment that connects as a different role must be the one inspected."""
    login, dangerous = escalation_roles
    admin_cursor.execute(f'GRANT "{dangerous}" TO "{login}"')

    # Configured as the exec role this time, not the scoped one.
    problems = _problems_with(PAC_DB_EXEC_USER=login)
    assert any(login in p and "BYPASSRLS" in p for p in problems), (
        f"the configured exec role was not inspected: {problems}"
    )


def test_the_real_configuration_has_an_intact_boundary(authtest_db):
    from app.db import verify_runtime_role_safety

    assert verify_runtime_role_safety() == []


def test_a_role_without_wac_is_reported_if_configured_as_the_exec_role(
    escalation_roles
):
    """The exec pool must be able to price; a silent failure there is also a bug."""
    login, _ = escalation_roles
    problems = _problems_with(PAC_DB_EXEC_USER=login)
    assert any("cannot SELECT sales.wac" in p for p in problems), problems


# ---------------------------------------------------------------------------
# Least privilege in the serving process
# ---------------------------------------------------------------------------

def test_serving_a_request_never_opens_the_owner_connection(authtest_db):
    """The API must not need owner credentials to answer a question.

    The owner role owns the protected tables and can create and drop objects.
    It was being used on every ask() to read one row from the dataset manifest,
    which meant the serving process had to hold those credentials and kept a
    pool of them open. Ingestion still uses the owner role; the API does not.
    """
    import secrets as _secrets

    from fastapi.testclient import TestClient

    from app.auth.identity import set_credential
    from app.db import _POOLS, close_pools, owner_transaction

    user_id = f"pactest-owner-check-{_secrets.token_hex(4)}"
    email = f"{user_id}@test.invalid"
    password = _secrets.token_urlsafe(18)
    with owner_transaction() as cur:
        cur.execute(
            "INSERT INTO users (user_id, email, full_name, role, can_view_wac) "
            "VALUES (%s, %s, 'Owner Pool Check', 'exec', 1)", (user_id, email))
    set_credential(user_id, password)

    try:
        # Start from nothing, so any owner pool below was opened by the app.
        close_pools()
        os.environ["PAC_COOKIE_SECURE"] = "false"
        from app.config import get_settings

        get_settings.cache_clear()

        from app.api.main import app

        with TestClient(app) as c:          # runs the startup boundary check
            assert c.post("/api/login",
                          json={"email": email, "password": password}).status_code == 200
            assert c.get("/api/me").status_code == 200
            assert c.post("/api/ask", json={
                "question": "What is our total revenue this quarter?"}).status_code == 200
            assert c.get("/api/conversations").status_code == 200

            opened = sorted(_POOLS)
            assert "owner" not in opened, (
                f"the serving process opened an owner connection: {opened}"
            )
            assert "auth" in opened and "exec" in opened
    finally:
        close_pools()
        with owner_transaction() as cur:
            cur.execute(
                "DELETE FROM app_conv.turns WHERE conversation_id IN "
                "(SELECT conversation_id FROM app_conv.conversations WHERE owner_user_id = %s)",
                (user_id,))
            cur.execute("DELETE FROM app_conv.conversations WHERE owner_user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_auth.sessions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_auth.credentials WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
