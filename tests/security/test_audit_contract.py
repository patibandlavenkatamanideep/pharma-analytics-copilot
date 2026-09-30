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
import re

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


# ---------------------------------------------------------------------------
# Every field the pipeline records reaches the table (review finding 7b)
# ---------------------------------------------------------------------------
#
# intent_gaps was set on the in-memory audit record and never written. A
# check of every key against the INSERT's column list found five more,
# added by later work -- turn_kind, prompt_version, planner_attempts,
# planner_repaired, usage_known -- each carried through the request and
# discarded at the write.

def test_every_declared_audit_column_exists():
    """The write is best-effort: a column missing from the table fails the
    whole INSERT, which is logged and swallowed, so the ENTIRE row vanishes
    for every request. This is the check that stops that being silent."""
    from app.db import auth_transaction
    from app.pipeline import AUDIT_COLUMNS

    with auth_transaction() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema = 'app_meta' AND table_name = 'query_audit'""")
        present = {r["column_name"] for r in cur.fetchall()}
    assert set(AUDIT_COLUMNS) <= present, sorted(set(AUDIT_COLUMNS) - present)


def test_every_audit_key_the_pipeline_sets_is_declared():
    """Read from the source, so a key set on a rare path is covered without
    a request having to reach it."""
    import pathlib
    import re

    from app.pipeline import AUDIT_COLUMNS, AUDIT_TRANSIENT

    source = pathlib.Path("app/pipeline.py").read_text()
    keys = set(re.findall(r'audit\["([a-z_]+)"\]', source))
    keys |= set(re.findall(r'"([a-z_]+)":', source.split("audit: dict[str, Any] = {")[1]
                                                    .split("}")[0]))
    undeclared = keys - set(AUDIT_COLUMNS) - AUDIT_TRANSIENT
    assert not undeclared, f"set on the audit record, never persisted: {sorted(undeclared)}"


def test_a_clarification_records_why_as_codes(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = ask(client, "What is the volume for FLOOBERTAX this quarter?")

    row = audit_rows(body["request_id"])[0]
    assert "unresolved_product" in (row["intent_gaps"] or [])
    assert row["reason_codes"][0] == "clarify"
    assert "unresolved_product" in row["reason_codes"]


def test_codes_are_codes_not_text_from_the_question(client, make_identity):
    """The question may name an account or a product. Codes must not carry
    it into the audit table."""
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = ask(client, "What is the volume for FLOOBERTAX this quarter?")

    row = audit_rows(body["request_id"])[0]
    # Non-empty first: over empty lists the loop below checks nothing, and
    # passed against the version that never wrote these columns.
    assert row["reason_codes"] and row["intent_gaps"]
    for code in row["reason_codes"] + row["intent_gaps"]:
        assert re.fullmatch(r"[a-z_]+", code), code
        assert "floobertax" not in code.lower()


def test_an_answer_records_how_it_was_planned(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = ask(client, "What is our total volume this quarter?")

    row = audit_rows(body["request_id"])[0]
    assert row["status"] == "answered"
    assert row["reason_codes"] == ["answered"]
    assert row["prompt_version"]
    assert row["planner_attempts"] >= 1
    assert row["planner_repaired"] is False
    assert row["turn_kind"] == "fresh_question"
    # Offline planning calls no provider. Zero tokens would be a measurement
    # nobody took, so usage is recorded as unknown.
    assert row["usage_known"] is False
