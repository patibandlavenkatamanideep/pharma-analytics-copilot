"""Every request leaves an audit row, and it never contains the sensitive part.

The audit trail is the answer to "who asked what, and what did the system
decide". Two properties have to hold together, and they pull in opposite
directions: it must record enough to reconstruct a decision, and it must not
become a second copy of the data the access controls exist to protect.

So: hashes, counts, timings and identities -- never a WAC amount, a result
row, a question, a prompt or a SQL statement.
"""

from __future__ import annotations

import json

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security


def audit_rows(request_id: str):
    from app.db import auth_transaction

    with auth_transaction() as cur:
        cur.execute(
            "SELECT * FROM app_meta.query_audit WHERE request_id = %s", (request_id,))
        return [dict(r) for r in cur.fetchall()]


def ask(client, question, **body):
    r = client.post("/api/ask", json={"question": question, **body})
    assert r.status_code in (200, 500), r.text
    return r.json()


def test_an_answered_request_is_audited(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = ask(client, "What is our total revenue this quarter?")

    rows = audit_rows(body["request_id"])
    assert len(rows) == 1, "no audit row for an answered request"
    row = rows[0]
    assert row["status"] == "answered"
    assert row["user_id"] == user.user_id
    assert row["role"] == "exec"
    assert row["wac_authorized"] is True
    assert row["dataset_id"]
    assert row["plan_hash"] and row["sql_hash"]
    assert row["row_count"] is not None


def test_a_refusal_is_audited_with_its_reason(client, make_identity, real_scopes):
    user = make_identity("ram", territory=real_scopes[0]["territory_name"],
                         region=real_scopes[0]["region_name"])
    sign_in(client, user)
    body = ask(client, f"Show me sales in the {real_scopes[1]['territory_name']} territory")

    rows = audit_rows(body["request_id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "denied"
    assert rows[0]["denial_reason"], "a refusal with no recorded reason"


def test_a_clarification_is_audited(client, make_identity):
    """An unresolvable entity is a decision too, and must not vanish."""
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = ask(client, "What is the volume for FLOOBERTAX this quarter?")

    rows = audit_rows(body["request_id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "clarify"


def test_the_audit_never_contains_the_question_sql_or_a_price(
    client, make_identity
):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    question = "What is our total revenue this quarter?"
    body = ask(client, question, include_sql=True)

    row = audit_rows(body["request_id"])[0]
    blob = json.dumps(row, default=str)

    assert question not in blob, "the audit stored the question text"
    assert "SELECT" not in blob.upper(), "the audit stored SQL"
    assert "$" not in blob, "the audit stored a currency amount"
    assert "wac" not in blob.replace("wac_authorized", ""), "the audit named the wac column"
    # The headline carries the figure; it must not be here either.
    headline = body["answer"]["headline"]
    assert headline not in blob


def test_the_sql_hash_is_a_hash_and_not_the_statement(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = ask(client, "What is our total revenue this quarter?", include_sql=True)
    row = audit_rows(body["request_id"])[0]
    assert len(row["sql_hash"]) <= 64
    assert " " not in row["sql_hash"]
    assert row["sql_hash"] not in body["sql"]


def test_two_requests_get_distinct_request_ids(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = ask(client, "What is our total revenue this quarter?")
    second = ask(client, "What is our total revenue this quarter?")
    assert first["request_id"] != second["request_id"]
    assert audit_rows(first["request_id"]) and audit_rows(second["request_id"])


def test_the_audit_records_the_contract_versions_in_force(client, make_identity):
    """A decision cannot be reconstructed without knowing which rules applied."""
    from app.analytics.registry import get_registry

    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = ask(client, "What is our total revenue this quarter?")
    row = audit_rows(body["request_id"])[0]
    assert row["metric_version"] == get_registry().version
    assert row["policy_version"]
