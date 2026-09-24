"""Failure paths.

The system is judged as much by what it does when something breaks as by its
happy path. The rule throughout: **never produce a number that did not come from
the database.** A failure must degrade into an explanation, never into a
plausible answer.
"""

from __future__ import annotations

import pytest

from app.analytics.plan import AnalyticalPlan
from app.llm.planner import PlannerError
from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]


# ---------------------------------------------------------------------------
# Planner failures
# ---------------------------------------------------------------------------

class BrokenPlanner:
    """A planner that always fails, as a provider outage would."""

    model_id = "broken-test-planner"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def plan(self, question, context):
        raise self.exc


def _pipeline_with(planner):
    from app.pipeline import Pipeline

    return Pipeline(planner)


@pytest.mark.parametrize(
    "exc",
    [
        PlannerError("planner produced an invalid plan twice: metric: unknown"),
        TimeoutError("read timed out"),
        ConnectionError("could not reach the provider"),
    ],
    ids=["invalid_plan", "timeout", "unreachable"],
)
def test_provider_failure_explains_instead_of_guessing(exc, exec_user):
    pipeline = _pipeline_with(BrokenPlanner(exc))
    if isinstance(exc, PlannerError):
        result = pipeline.ask(exec_user, "What are our top accounts?")
        assert result.status == "error"
        assert result.answer is None
        # The message must help, not just apologise.
        assert "try" in result.message.lower() or "example" in result.message.lower()
        # And it must not leak the provider's internals to the user.
        assert "planner produced an invalid plan" not in result.message
    else:
        # A non-PlannerError propagates to the API layer, which turns it into a
        # 500 with a safe message; it must NOT be silently swallowed into an
        # answer.
        with pytest.raises(type(exc)):
            pipeline.ask(exec_user, "What are our top accounts?")


def test_a_failed_request_is_still_audited(exec_user):
    """An outage must not create a hole in the audit trail."""
    from app.db import auth_transaction

    with auth_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM app_meta.query_audit")
        before = cur.fetchone()["n"]

    pipeline = _pipeline_with(BrokenPlanner(PlannerError("provider exploded")))
    pipeline.ask(exec_user, "What are our top accounts?")

    with auth_transaction() as cur:
        cur.execute(
            "SELECT status, denial_reason FROM app_meta.query_audit "
            "ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM app_meta.query_audit")
        after = cur.fetchone()["n"]

    assert after == before + 1
    assert row["status"] == "planner_error"
    assert "provider exploded" in (row["denial_reason"] or "")


# ---------------------------------------------------------------------------
# Database failures
# ---------------------------------------------------------------------------

def test_query_timeout_returns_advice_not_an_answer(pipeline, exec_user, monkeypatch):
    """A cancelled query must not become a partial or invented result."""
    from app.analytics import compiler as compiler_module

    original = compiler_module.Compiler.compile

    def slow(self, plan, *, anchor):
        query = original(self, plan, anchor=anchor)
        # An unindexable cross product; the 5s budget will cancel it.
        query.sql = (
            "SELECT count(*) AS value FROM sales a, sales b "
            "WHERE a.pack_units + b.pack_units > 0"
        )
        query.params = []
        return query

    monkeypatch.setattr(compiler_module.Compiler, "compile", slow)

    result = pipeline.ask(exec_user, "What are our pack units this quarter?")
    assert result.status == "error"
    assert result.answer is None
    assert "too long" in result.message.lower() or "narrow" in result.message.lower()


def test_database_unavailable_is_reported_not_faked(pipeline, exec_user, monkeypatch):
    import app.pipeline as pipeline_module

    class Unavailable:
        def __call__(self, *args, **kwargs):
            raise OSError("connection refused")

        def __enter__(self):
            raise OSError("connection refused")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pipeline_module, "analytics_transaction", Unavailable())
    result = pipeline.ask(exec_user, "What are our pack units this quarter?")

    assert result.status == "error"
    assert result.answer is None
    assert "unavailable" in result.message.lower() or "try again" in result.message.lower()
    # No fabricated figure anywhere in the user-visible text.
    assert not any(ch.isdigit() for ch in result.message.replace("try again", ""))


# ---------------------------------------------------------------------------
# Empty and degenerate results
# ---------------------------------------------------------------------------

def test_no_rows_is_not_reported_as_zero(compiler, anchor, exec_user):
    """Absent observations and a verified zero are different claims."""
    from app.analytics.render import render
    from app.auth.policy import scope_note
    from tests.conftest import run_plan

    plan = AnalyticalPlan.model_validate(
        {
            "metric": "paid_pack_units",
            "dimensions": ["account"],
            # A product that does not exist, so nothing can match.
            "filters": {"product_names": ["NOT-A-REAL-DRUG"]},
            "time": {"kind": "named", "named": "r3m"},
        }
    )
    rows, query = run_plan(compiler, plan, anchor, exec_user)
    answer = render(rows, query, plan, scope_note=scope_note(exec_user, plan), max_rows=100)

    assert answer.row_count == 0
    combined = (answer.headline + " ".join(answer.warnings)).lower()
    assert "no " in combined
    assert "not the same as a confirmed zero" in " ".join(answer.warnings).lower()


def test_unknown_entity_does_not_become_a_guess(pipeline, exec_user):
    result = pipeline.ask(exec_user, "How is Zenovaxx performing this quarter?")
    # Either it reports nothing found, or it answers a broader question and says
    # so -- what it must never do is silently substitute a similar product.
    if result.answer and result.answer.row_count:
        text = " ".join(result.answer.notes + result.answer.warnings).lower()
        assert text, "a substituted interpretation must be disclosed"


# ---------------------------------------------------------------------------
# Oversized and malformed input
# ---------------------------------------------------------------------------

def test_an_oversized_question_is_rejected_by_the_schema():
    from pydantic import ValidationError

    from app.api.main import AskRequest

    with pytest.raises(ValidationError):
        AskRequest(question="x" * 5000)
    with pytest.raises(ValidationError):
        AskRequest(question="")


def test_a_plan_with_an_unbounded_limit_cannot_be_built():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnalyticalPlan.model_validate(
            {
                "metric": "paid_pack_units",
                "dimensions": ["account"],
                "time": {"kind": "named", "named": "r3m"},
                "ranking": {"direction": "top", "limit": 10**9},
            }
        )


# ---------------------------------------------------------------------------
# Dataset availability
# ---------------------------------------------------------------------------

def test_readiness_requires_a_published_dataset(pipeline):
    """A partially loaded refresh must never be queryable."""
    dataset = pipeline.current_dataset()
    assert dataset["dataset_id"]

    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM app_meta.dataset_manifest "
            "WHERE load_state = 'published'"
        )
        # Exactly one snapshot is published at a time; publishing supersedes
        # the previous one in the same transaction.
        assert cur.fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------------

def test_the_live_schema_matches_the_contract():
    from app.data.schema_contract import check
    from app.db import owner_transaction

    with owner_transaction() as cur:
        result = check(cur)
    assert result.compatible, result.report()
    assert len(result.fingerprint) == 16


def test_an_incompatible_schema_is_refused_with_a_useful_message():
    """A renamed or retyped column must fail loudly, naming what differs."""
    from app.data import schema_contract

    observed = {
        "organizations": {"org_id": "text", "org_name": "text"},        # truncated
        "products": dict(schema_contract.REQUIRED["products"]),
        "sales": {**schema_contract.REQUIRED["sales"], "pack_units": "text"},  # retyped
        "zip_territory": dict(schema_contract.REQUIRED["zip_territory"]),
        # "users" absent entirely
    }

    class FakeCursor:
        def execute(self, *a, **k):
            self._rows = [
                {"table_name": t, "column_name": c, "data_type": d}
                for t, cols in observed.items() for c, d in cols.items()
            ]

        def fetchall(self):
            return self._rows

    result = schema_contract.check(FakeCursor())
    assert not result.compatible

    report = result.report()
    assert "users" in report                      # the missing table is named
    assert "organizations.zip" in report          # a missing column is named
    assert "pack_units" in report                 # the retyped column is named
    assert "Not supported: a different schema" in report

    with pytest.raises(schema_contract.SchemaIncompatible):
        schema_contract.require_compatible(FakeCursor())


def test_an_extra_column_is_compatible():
    """Additive change must not break a working deployment."""
    from app.data import schema_contract

    observed = {t: dict(c) for t, c in schema_contract.REQUIRED.items()}
    observed["organizations"]["some_new_column"] = "text"

    class FakeCursor:
        def execute(self, *a, **k):
            self._rows = [
                {"table_name": t, "column_name": c, "data_type": d}
                for t, cols in observed.items() for c, d in cols.items()
            ]

        def fetchall(self):
            return self._rows

    result = schema_contract.check(FakeCursor())
    assert result.compatible
    assert "organizations.some_new_column" in result.extra_columns
    # An ignored column must not change the fingerprint, so stored results
    # remain traceable across an additive migration.
    baseline = schema_contract.fingerprint(
        {t: dict(c) for t, c in schema_contract.REQUIRED.items()}
    )
    assert result.fingerprint == baseline
