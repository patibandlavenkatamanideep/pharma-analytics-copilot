"""What the HTTP layer is allowed to reveal, on every path.

Exercised over real HTTP with real sessions against the disposable database.
The four response shapes -- answered, denied, clarify, error -- are all
reachable here, the last two by substituting the planner, because they are
contract obligations of the API rather than behaviours of any one planner.

Two properties matter on every path:

  * SQL and internal detail appear only when the caller explicitly asked for
    SQL, and even then only the statement their own principal was authorized
    to run.
  * A failure is reported without a stack trace, a driver message, a table
    name or a connection string, and still carries a request id so the real
    cause can be found in the log.
"""

from __future__ import annotations

import json

import pytest

from tests.security.helpers import sign_in

INTERNALS = (
    "Traceback", "psycopg", "postgresql://", "password", "app_auth",
    "pac_scoped_login", "pac_exec_login", "sqlstate", "SELECT ",
)


def assert_no_internals(payload: dict) -> None:
    body = json.dumps(payload)
    for marker in INTERNALS:
        assert marker.lower() not in body.lower(), f"response leaked {marker!r}: {body[:400]}"


# ---------------------------------------------------------------------------
# include_sql
# ---------------------------------------------------------------------------

def test_sql_and_plan_are_absent_unless_requested(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    r = client.post("/api/ask",
                    json={"question": "What is our total revenue this quarter?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered"
    assert "sql" not in body, "SQL was returned without being asked for"
    assert "plan" not in body, "the plan was returned without being asked for"
    assert body["request_id"]
    assert_no_internals(body)


def test_requested_sql_is_returned_with_the_plan(client, make_identity):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    body = client.post("/api/ask", json={
        "question": "What is our total revenue this quarter?", "include_sql": True,
    }).json()
    assert body["status"] == "answered"
    assert "sql" in body and "select" in body["sql"].lower()
    assert "plan" in body and body["plan"]["metric"]


def test_requested_sql_never_contains_pricing_for_an_unauthorized_user(
    client, make_identity, real_scopes
):
    """The escape hatch must not become the leak: a RAM asking to see the SQL
    sees the statement THEY were authorized to run, which cannot mention wac."""
    user = make_identity("ram", territory=real_scopes[0]["territory_name"],
                         region=real_scopes[0]["region_name"])
    sign_in(client, user)
    body = client.post("/api/ask", json={
        "question": "What is my total revenue in dollars this quarter?",
        "include_sql": True,
    }).json()
    if body.get("sql"):
        assert "wac" not in body["sql"].lower(), f"pricing column in a RAM's SQL: {body['sql']}"
    assert "$" not in json.dumps(body)


# ---------------------------------------------------------------------------
# denied
# ---------------------------------------------------------------------------

def test_denied_is_a_200_with_a_reason_and_no_internals(
    client, make_identity, real_scopes
):
    """A refusal is an answer about authorization, not a transport error."""
    user = make_identity("ram", territory=real_scopes[0]["territory_name"],
                         region=real_scopes[0]["region_name"])
    sign_in(client, user)
    body = client.post("/api/ask", json={
        "question": f"Show me sales in the {real_scopes[1]['territory_name']} territory",
        "include_sql": True,
    }).json()
    assert body["status"] == "denied"
    assert body["message"]
    assert body["request_id"]
    # A denial must not hand back the statement it refused to run.
    assert "sql" not in body or not body["sql"]
    assert_no_internals(body)


# ---------------------------------------------------------------------------
# clarify and error, via a substituted planner
# ---------------------------------------------------------------------------

class _StubPlanner:
    model_id = "stub"

    def __init__(self, behaviour):
        self.behaviour = behaviour

    def plan(self, question, context):
        from app.analytics.plan import AnalyticalPlan, MetricKey, TimeWindow

        if self.behaviour == "clarify":
            # A clarification still has to carry a metric and a time window,
            # because the schema requires them. The planner prompt says to set
            # "clarification (and nothing else)", which the type does not
            # actually allow -- recorded as R09 (clarification union contract)
            # and not addressed in this phase.
            return AnalyticalPlan(
                metric=MetricKey.paid_pack_units,
                time=TimeWindow(kind="named", named="r3m"),
                clarification="Which product did you mean?",
            )
        detail = "connection reset by peer while calling postgresql://secret@host"
        if self.behaviour == "planner-error":
            from app.llm.planner import PlannerError

            raise PlannerError(detail)
        raise RuntimeError(detail)


@pytest.fixture
def substituted_planner(client):
    """Swap the planner the running app uses, and put it back afterwards."""
    import app.api.main as main
    from app.pipeline import Pipeline

    original = main._pipeline

    def use(behaviour):
        main._pipeline = Pipeline(_StubPlanner(behaviour))

    yield use
    main._pipeline = original


def test_clarify_returns_a_question_and_no_answer(
    client, make_identity, substituted_planner
):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    substituted_planner("clarify")

    body = client.post("/api/ask", json={"question": "How did it do?",
                                         "include_sql": True}).json()
    assert body["status"] == "clarify"
    assert body["message"] == "Which product did you mean?"
    assert "answer" not in body, "a clarification came with results attached"
    assert not body.get("sql"), "a clarification came with SQL attached"
    assert body["request_id"]


def test_a_planner_failure_is_reported_without_internals(
    client, make_identity, substituted_planner
):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    substituted_planner("raise")

    r = client.post("/api/ask", json={"question": "What is our revenue?"})
    # The pipeline handles PlannerError itself and returns status "error" with
    # a 200. Anything else is genuinely unexpected and becomes a 500 -- which
    # is correct, but it still has to be investigable and still must not
    # describe the cause.
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["request_id"], "an error with no request id cannot be investigated"
    assert detail["message"] == "Something went wrong answering that. Please try again."
    assert_no_internals(r.json())
    assert "secret" not in json.dumps(r.json()).lower()


def test_a_declared_planner_failure_is_an_answerable_error(
    client, make_identity, substituted_planner
):
    """A PlannerError is an expected outcome, so it stays inside the contract."""
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    substituted_planner("planner-error")

    r = client.post("/api/ask", json={"question": "What is our revenue?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["request_id"]
    assert_no_internals(body)
    assert "secret" not in json.dumps(body).lower()


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/me", None),
    ("get", "/api/conversations", None),
    ("get", "/api/conversations/c_anything", None),
    ("post", "/api/ask", {"question": "revenue"}),
])
def test_protected_endpoints_require_a_session(client, method, path, body):
    client.cookies.clear()
    r = client.request(method.upper(), path, json=body)
    assert r.status_code == 401, f"{path} answered without a session"
    assert_no_internals(r.json())


def test_health_and_ready_need_no_session(client):
    client.cookies.clear()
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code in (200, 503)
