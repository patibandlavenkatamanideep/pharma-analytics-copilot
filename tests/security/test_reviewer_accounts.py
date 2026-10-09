"""Only accounts the owner approves may use the application.

Staging's sign-in page is reachable from anywhere (the owner's decision of
9 October 2026); there is no public sign-up, so the accounts
scripts/provision_reviewers.py creates are the only way in. Each reviewer
gets an account of their own with the scope of an existing user the owner
names, a disabled reviewer is signed out at once, and nothing the script
prints carries a password. Against the disposable authorization database.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import subprocess
import sys

import pytest

pytestmark = pytest.mark.security

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def run(reviewers) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PAC_REVIEWERS": json.dumps(reviewers)}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "provision_reviewers.py")],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def users_like(role: str) -> dict:
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT user_id, role, territory_name, region_name, can_view_wac FROM users "
                    "WHERE role = %s AND user_id NOT LIKE 'rev-%%' ORDER BY user_id LIMIT 1", (role,))
        return dict(cur.fetchone())


@pytest.fixture
def reviewer_emails(authtest_db):
    """Disposable reviewer addresses; every account made for them is removed."""
    from app.db import owner_transaction
    from scripts.provision_reviewers import reviewer_id

    emails = [f"reviewer-{secrets.token_hex(4)}@test.invalid" for _ in range(2)]
    yield emails
    ids = [reviewer_id(e) for e in emails]
    with owner_transaction() as cur:
        cur.execute("DELETE FROM app_conv.turns WHERE conversation_id IN (SELECT conversation_id "
                    "FROM app_conv.conversations WHERE owner_user_id = ANY(%s))", (ids,))
        cur.execute("DELETE FROM app_conv.conversations WHERE owner_user_id = ANY(%s)", (ids,))
        for table in ("app_auth.sessions", "app_auth.credentials"):
            cur.execute(f"DELETE FROM {table} WHERE user_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM app_auth.login_attempts WHERE email = ANY(%s)", (emails,))
        cur.execute("DELETE FROM users WHERE user_id = ANY(%s)", (ids,))


def test_each_reviewer_gets_an_own_account_with_the_scope_named(reviewer_emails):
    from app.auth.identity import authenticate
    from scripts.provision_reviewers import reviewer_id

    exec_like, ram_like = users_like("exec"), users_like("ram")
    passwords = [secrets.token_urlsafe(18) for _ in reviewer_emails]
    result = run([
        {"email": reviewer_emails[0], "name": "Reviewer One", "like": exec_like["user_id"],
         "password": passwords[0]},
        {"email": reviewer_emails[1], "name": "Reviewer Two", "like": ram_like["user_id"],
         "password": passwords[1]},
    ])
    assert result.returncode == 0, result.stderr
    assert "2 active" in result.stdout
    assert not any(p in result.stdout + result.stderr for p in passwords)

    for email, password, like in zip(reviewer_emails, passwords, (exec_like, ram_like)):
        _, principal = authenticate(email, password)
        assert principal.user_id == reviewer_id(email) != like["user_id"]
        assert principal.role == like["role"]
    # Run again: the same accounts, updated, not duplicated.
    assert run([{"email": reviewer_emails[0], "name": "Reviewer One", "like": exec_like["user_id"],
                 "password": passwords[0]}]).returncode == 0


def test_a_disabled_reviewer_is_signed_out_and_refused(reviewer_emails):
    from app.auth.identity import authenticate, resolve

    like = users_like("exec")["user_id"]
    password = secrets.token_urlsafe(18)
    email = reviewer_emails[0]
    assert run([{"email": email, "name": "Reviewer", "like": like, "password": password}]).returncode == 0
    session, _ = authenticate(email, password)
    assert resolve(session.token) is not None

    result = run([{"email": email, "name": "Reviewer", "disabled": True}])
    assert result.returncode == 0 and "1 disabled" in result.stdout
    assert resolve(session.token) is None
    with pytest.raises(Exception):
        authenticate(email, password)


def test_running_the_list_again_leaves_reviewers_signed_in(reviewer_emails):
    """Adding a reviewer mid-week re-runs the whole list; the reviewers
    already at work keep their sessions. A changed password still ends them."""
    from app.auth.identity import authenticate, resolve

    like = users_like("exec")["user_id"]
    first = {"email": reviewer_emails[0], "name": "One", "like": like,
             "password": secrets.token_urlsafe(18)}
    assert run([first]).returncode == 0
    session, _ = authenticate(first["email"], first["password"])

    second = {"email": reviewer_emails[1], "name": "Two", "like": like,
              "password": secrets.token_urlsafe(18)}
    assert run([first, second]).returncode == 0
    assert resolve(session.token) is not None

    rotated = {**first, "password": secrets.token_urlsafe(18)}
    assert run([rotated, second]).returncode == 0
    assert resolve(session.token) is None
    authenticate(rotated["email"], rotated["password"])


@pytest.mark.parametrize("bad", [
    {"email": "not-an-address", "name": "X", "like": "U001", "password": "p" * 16},
    {"email": "a@test.invalid", "name": "", "like": "U001", "password": "p" * 16},
    {"email": "a@test.invalid", "name": "X", "like": "", "password": "p" * 16},
    {"email": "a@test.invalid", "name": "X", "like": "U001", "password": "short"},
])
def test_an_invalid_list_changes_nothing(bad, reviewer_emails):
    from app.db import owner_transaction

    good = {"email": reviewer_emails[0], "name": "Good", "like": users_like("exec")["user_id"],
            "password": secrets.token_urlsafe(18)}
    result = run([good, bad])
    assert result.returncode == 2 and "refused" in result.stderr
    with owner_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM users WHERE email = %s", (reviewer_emails[0],))
        assert cur.fetchone()["n"] == 0


def test_a_scope_that_does_not_exist_is_refused(reviewer_emails):
    result = run([{"email": reviewer_emails[0], "name": "X", "like": "no-such-user",
                   "password": secrets.token_urlsafe(18)}])
    assert result.returncode == 2 and "no user" in result.stderr


def merge_csv(tmp_path, current, csv_text) -> subprocess.CompletedProcess:
    path = tmp_path / "reviewers.csv"
    path.write_text(csv_text)
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "provision_reviewers.py"),
                           "--merge-csv", str(path)], cwd=ROOT, input=json.dumps(current),
                          capture_output=True, text=True, timeout=60)


def test_the_owner_list_keeps_passwords_and_disables_who_was_removed(tmp_path):
    current = [{"email": "kept@test.invalid", "name": "Kept", "like": "U1", "password": "k" * 24},
               {"email": "gone@test.invalid", "name": "Gone", "like": "U1", "password": "g" * 24}]
    result = merge_csv(tmp_path, current, "email,name,like,disabled\n"
                       "Kept@test.invalid,Kept,U2,\n"
                       "new@test.invalid,New,U1,\n"
                       "paused@test.invalid,Paused,U1,yes\n")
    assert result.returncode == 0, result.stderr
    merged = {r["email"]: r for r in json.loads(result.stdout)}
    assert merged["kept@test.invalid"] == {"email": "kept@test.invalid", "name": "Kept",
                                           "like": "U2", "password": "k" * 24}
    assert len(merged["new@test.invalid"]["password"]) >= 12
    assert merged["paused@test.invalid"].get("disabled") is True
    assert merged["gone@test.invalid"] == {"email": "gone@test.invalid", "name": "Gone",
                                           "disabled": True}
    assert "2 active (1 new), 2 disabled" in result.stderr
    assert merged["new@test.invalid"]["password"] not in result.stderr


@pytest.mark.parametrize("csv_text", [
    "email,name\nx@test.invalid,X\n",
    "email,name,like\nnot-an-address,X,U1\n",
    "email,name,like\nx@test.invalid,X,U1\nX@test.invalid,Y,U1\n",
])
def test_an_invalid_owner_list_writes_nothing(tmp_path, csv_text):
    result = merge_csv(tmp_path, [], csv_text)
    assert result.returncode == 2 and "refused" in result.stderr and result.stdout == ""
