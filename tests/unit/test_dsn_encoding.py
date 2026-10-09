"""A generated password must not be able to change what the DSN means.

The connection string was built by interpolating into a URL:

    postgresql://{user}:{password}@{host}:{port}/{name}

A password containing ``@`` splits the authority, so the real host becomes
part of the password and the client connects somewhere else -- or fails in a
way that looks like a credential problem. ``/`` ends the authority, ``?``
starts a query, ``#`` starts a fragment. ``bootstrap_db.py`` generates
passwords, so this is not a hypothetical input.

These tests never print, log or assert a credential's value. They assert what
the DSN *means* -- by parsing it back with the same library that consumes it
-- and where a value must be absent, they assert absence.
"""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.config import Settings

# Every character that is structural in a URL, plus quoting characters that
# are structural in the keyword/value form make_conninfo emits.
RESERVED = [
    pytest.param("p@ssword", id="at-sign-splits-the-authority"),
    pytest.param("pa:ss", id="colon-separates-user-from-password"),
    pytest.param("pa/ss", id="slash-ends-the-authority"),
    pytest.param("pa?ss", id="question-mark-starts-a-query"),
    pytest.param("pa#ss", id="hash-starts-a-fragment"),
    pytest.param("pa ss", id="space"),
    pytest.param("pa'ss", id="single-quote-is-structural-in-keyword-form"),
    pytest.param("pa\\ss", id="backslash-escapes-in-keyword-form"),
    pytest.param("p@ss:w/rd?x#y 'z\\", id="all-of-them-at-once"),
    pytest.param("percent%20encoded", id="literal-percent-is-not-pre-encoded"),
]


def settings_with(password: str) -> Settings:
    return Settings(
        db_host="db.internal", db_port=6543, db_name="pharma_analytics",
        db_owner_user="pac_owner", db_owner_password=password,
        db_auth_user="pac_auth_login", db_auth_password=password,
        db_exec_user="pac_exec_login", db_exec_password=password,
        db_scoped_user="pac_scoped_login", db_scoped_password=password,
    )


@pytest.mark.parametrize("password", RESERVED)
def test_the_dsn_still_points_at_the_right_host_and_database(password):
    """The failure that matters: connecting somewhere you did not intend."""
    parsed = conninfo_to_dict(settings_with(password).dsn("owner"))

    assert parsed["host"] == "db.internal"
    assert str(parsed["port"]) == "6543"
    assert parsed["dbname"] == "pharma_analytics"
    assert parsed["user"] == "pac_owner"


@pytest.mark.parametrize("password", RESERVED)
def test_the_password_round_trips_exactly(password):
    """Asserted by equality against the input, never by printing it."""
    parsed = conninfo_to_dict(settings_with(password).dsn("owner"))
    assert parsed["password"] == password


@pytest.mark.parametrize("role", ["owner", "auth", "exec", "scoped"])
def test_every_role_gets_its_own_user(role):
    parsed = conninfo_to_dict(settings_with("x").dsn(role))
    assert parsed["user"] == {
        "owner": "pac_owner", "auth": "pac_auth_login",
        "exec": "pac_exec_login", "scoped": "pac_scoped_login",
    }[role]


def test_an_empty_password_is_omitted_rather_than_sent_empty():
    """Local peer/trust authentication has no password. Sending an empty one
    is a different thing from sending none."""
    parsed = conninfo_to_dict(settings_with("").dsn("owner"))
    assert "password" not in parsed


def test_a_password_cannot_inject_another_connection_parameter():
    """The attack the URL form allowed: smuggling a second host."""
    hostile = "x' host='evil.example.com"
    parsed = conninfo_to_dict(settings_with(hostile).dsn("owner"))

    assert parsed["host"] == "db.internal", "a password overrode the host"
    assert parsed["password"] == hostile


def test_a_password_cannot_smuggle_sslmode():
    """Downgrading TLS through a credential would be worse than a wrong host."""
    hostile = "x' sslmode='disable"
    parsed = conninfo_to_dict(settings_with(hostile).dsn("owner"))
    assert parsed.get("sslmode") in (None, "prefer", "require", "verify-full")
    assert parsed["password"] == hostile


@pytest.mark.parametrize("password", RESERVED)
def test_the_dsn_is_keyword_form_not_a_url(password):
    """make_conninfo emits keyword/value pairs, which have no authority
    section for a credential to break out of."""
    dsn = settings_with(password).dsn("owner")
    assert not dsn.startswith("postgres")
    assert "host=" in dsn and "dbname=" in dsn
