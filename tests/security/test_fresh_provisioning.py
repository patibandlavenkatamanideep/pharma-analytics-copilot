"""A database provisioned once must be the database the tests run against.

Every other test runs on databases that have been migrated many times.
Migrations are re-run in full on every `migrate.py`, so a grant that only
takes effect on the SECOND pass -- a blanket "ON ALL TABLES" that runs before
a later migration creates its table -- is invisible to them. That is how a
first deployment could not sign anyone in: app_auth.login_attempts was
granted to the serving role only on a second pass, and the long-lived local
databases also carried a hand-applied grant. The image test found it on a
fresh database.

This provisions a disposable database in one pass and asserts:

* its privileges are exactly what a second pass would make them -- no
  grant depends on running migrations twice;
* signing in works on it.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import subprocess
import sys

import pytest

pytestmark = pytest.mark.security

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRESH = os.environ.get("PAC_FRESHTEST_DB", "pharma_analytics_freshtest")
ROLES = ("pac_auth", "pac_auth_login", "pac_rt_exec", "pac_rt_scoped",
         "pac_exec_login", "pac_scoped_login")

MATRIX = """
SELECT r.role || ' ' || n.nspname || '.' || c.relname || ' ' || p.priv AS grant
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(%(roles)s::text[]) AS r(role)
CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) AS p(priv)
WHERE c.relkind IN ('r', 'v', 'p')
  AND n.nspname IN ('public', 'app_auth', 'app_conv', 'app_meta', 'app_graph',
                    'app_ingest', 'app_ref')
  AND has_table_privilege(r.role, c.oid, p.priv)
UNION ALL
SELECT r.role || ' ' || n.nspname || '.' || c.relname || ' ' || p.priv
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(%(roles)s::text[]) AS r(role)
CROSS JOIN (VALUES ('USAGE'), ('UPDATE')) AS p(priv)
WHERE c.relkind = 'S'
  AND n.nspname IN ('public', 'app_auth', 'app_conv', 'app_meta', 'app_graph',
                    'app_ingest', 'app_ref')
  AND has_sequence_privilege(r.role, c.oid, p.priv)
ORDER BY 1
"""


def run(script: str, *args: str) -> None:
    env = {**os.environ, "PAC_DB_NAME": FRESH, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-2000:])


@pytest.fixture(scope="module")
def fresh_db():
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools

    if FRESH in ("pharma_analytics", os.environ.get("PAC_DB_NAME")):
        pytest.fail("refusing to provision over a working database")
    run("bootstrap_db.py", "--drop", "--no-env")

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = FRESH
    get_settings.cache_clear()
    close_pools()
    clear_caches()
    try:
        yield
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


def matrix() -> list[str]:
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute(MATRIX, {"roles": list(ROLES)})
        return [r["grant"] for r in cur.fetchall()]


def test_no_grant_depends_on_migrating_twice(fresh_db):
    from app.db import close_pools

    once = matrix()
    close_pools()
    run("migrate.py")
    twice = matrix()
    assert sorted(set(twice) - set(once)) == [], "granted only by a second migration pass"
    assert sorted(set(once) - set(twice)) == []


def test_a_freshly_provisioned_database_can_sign_someone_in(fresh_db):
    from app.auth.identity import authenticate, set_credential
    from app.db import owner_transaction

    user_id = f"pacfresh-{secrets.token_hex(4)}"
    email, password = f"{user_id}@fresh.invalid", secrets.token_urlsafe(18)
    with owner_transaction() as cur:
        cur.execute("INSERT INTO users (user_id, email, full_name, role, can_view_wac) "
                    "VALUES (%s, %s, 'Fresh', 'exec', 1)", (user_id, email))
    set_credential(user_id, password)
    session, principal = authenticate(email, password, user_agent="test", ip="127.0.0.1")
    assert principal.user_id == user_id and session.token


def test_a_freshly_provisioned_database_signs_someone_in_through_sso(fresh_db, monkeypatch):
    """Single sign-on on a database migrated once, browser binding included:
    the attempt is stored, a callback from another browser is refused before
    the code is redeemed, and the starting browser is signed in. The provider
    is the in-process one (tests/security/fake_idp.py); a real IdP is a
    staging check (docs/RUNBOOK.md)."""
    import httpx2 as httpx

    from app.auth.oidc import OIDCError, Provider
    from app.config import get_settings
    from app.db import owner_transaction
    from tests.security.fake_idp import CLIENT, ISSUER, REDIRECT, FakeIdP

    for name, value in {"PAC_OIDC_ENABLED": "true", "PAC_OIDC_ISSUER": ISSUER,
                        "PAC_OIDC_CLIENT_ID": CLIENT, "PAC_OIDC_REDIRECT_URI": REDIRECT}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    user_id = f"pacfresh-{secrets.token_hex(4)}"
    with owner_transaction() as cur:
        cur.execute("INSERT INTO users (user_id, email, full_name, role, can_view_wac) "
                    "VALUES (%s, %s, 'Fresh SSO', 'exec', 1)", (user_id, f"{user_id}@fresh.invalid"))
        cur.execute("INSERT INTO app_auth.identities (issuer, subject, user_id) "
                    "VALUES (%s, %s, %s)", (ISSUER, "sub-fresh", user_id))
    idp = FakeIdP()
    provider = Provider(transport=httpx.MockTransport(idp.handler))
    begun = provider.begin("/")
    code, state = idp.authorize(begun.authorization_url, sub="sub-fresh")

    with pytest.raises(OIDCError) as refused:
        provider.complete(code=code, state=state, browser=secrets.token_urlsafe(32))
    assert refused.value.code == "browser_mismatch" and code in idp.codes

    token, _, principal, target = provider.complete(code=code, state=state,
                                                    browser=begun.binding)
    assert principal.user_id == user_id and token and target == "/"
    monkeypatch.undo()
    get_settings.cache_clear()
