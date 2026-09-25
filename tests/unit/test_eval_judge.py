"""Adversarial tests for the evaluation judge.

An evaluation is only worth its false-positive rate. These tests do not ask
whether the system answers well; they hand the judge results that are plainly
WRONG and require it to say so. A judge that cannot fail is a judge that
cannot report a score.

Every case here passed the original judge.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _load_runner():
    """Import scripts/run_evals.py, which is a script rather than a package."""
    spec = importlib.util.spec_from_file_location(
        "run_evals", ROOT / "scripts" / "run_evals.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_evals"] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ---------------------------------------------------------------------------
# Minimal stand-ins, so the judge is tested and nothing else is
# ---------------------------------------------------------------------------

@dataclass
class FakeAnswer:
    headline: str = ""
    table: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    scope_note: str = ""
    period_note: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


@dataclass
class FakeResult:
    status: str = "answered"
    conversation_id: str = "c_test"
    message: str = ""
    answer: FakeAnswer | None = None
    alternative: str | None = None
    plan: dict[str, Any] | None = None
    sql: str | None = None


class FakePrincipal:
    scope_kind = "global"
    scope_value = None
    wac_authorized = True


INCOMPLETE_PERIOD = (
    "This window includes the current reporting period, which is still "
    "accumulating."
)


def verdict(spec, result, *, tolerance=0.001):
    ok, reason = runner.judge(spec, result, FakePrincipal(), tolerance)
    return ok, reason


# ---------------------------------------------------------------------------
# A qualifying note must qualify SOMETHING
# ---------------------------------------------------------------------------

def test_boilerplate_note_does_not_satisfy_answered_with_note():
    """amb-01 as it actually behaved.

    The question asks for GENERIC share; the system returned company BRAND
    share, 71.79%. It passed because `answered_with_note` accepted any note at
    all, and every answer covering the current period carries the
    incomplete-period note. The note has nothing to do with the ambiguity.
    """
    result = FakeResult(
        answer=FakeAnswer(
            headline="Brand market share: 71.79%",
            notes=[INCOMPLETE_PERIOD],
            row_count=1,
        )
    )
    spec = {
        "type": "any_of",
        "allowed": ["clarify", "denied", "answered_with_note"],
        "note_contains": ["generic", "derived", "classification"],
    }
    ok, reason = verdict(spec, result)
    assert not ok, f"boilerplate accepted as a qualifying note: {reason}"


def test_a_note_that_addresses_the_ambiguity_is_accepted():
    result = FakeResult(
        answer=FakeAnswer(
            headline="Generic share: 28.10%",
            notes=[
                INCOMPLETE_PERIOD,
                "Generic status is derived from the product classification, "
                "because brand_flag = 0 also covers branded competitors.",
            ],
            row_count=1,
        )
    )
    spec = {
        "type": "any_of",
        "allowed": ["clarify", "denied", "answered_with_note"],
        "note_contains": ["generic", "derived", "classification"],
    }
    ok, reason = verdict(spec, result)
    assert ok, reason


def test_any_of_requires_note_contains_when_answered_with_note_is_allowed():
    """An unqualified `answered_with_note` is what made amb-01 vacuous."""
    result = FakeResult(answer=FakeAnswer(notes=[INCOMPLETE_PERIOD], row_count=1))
    spec = {"type": "any_of", "allowed": ["clarify", "answered_with_note"]}
    with pytest.raises(ValueError, match="note_contains"):
        verdict(spec, result)


# ---------------------------------------------------------------------------
# nonempty must not stand in for a semantic check
# ---------------------------------------------------------------------------

def test_nonempty_rejects_the_wrong_metric():
    """b340-01: the question asks for a PERCENTAGE and got 59,419 packs."""
    result = FakeResult(
        answer=FakeAnswer(headline="Paid pack units: 59,419 packs", row_count=1),
        plan={"metric": "paid_pack_units"},
    )
    spec = {"type": "nonempty", "metric": "share_340b"}
    ok, reason = verdict(spec, result)
    assert not ok, f"a packs answer satisfied a percentage question: {reason}"


def test_nonempty_requires_a_declared_metric():
    result = FakeResult(answer=FakeAnswer(row_count=3), plan={"metric": "anything"})
    with pytest.raises(ValueError, match="metric"):
        verdict({"type": "nonempty"}, result)


def test_nonempty_accepts_the_declared_metric():
    result = FakeResult(
        answer=FakeAnswer(headline="340B share: 12.40%", row_count=1),
        plan={"metric": "share_340b"},
    )
    ok, reason = verdict({"type": "nonempty", "metric": "share_340b"}, result)
    assert ok, reason


# ---------------------------------------------------------------------------
# An expectation that cannot fail is not an expectation
# ---------------------------------------------------------------------------

def test_warning_with_no_needle_is_rejected_as_a_specification():
    """`type: warning` with no `contains` passed an answer carrying NO warning."""
    result = FakeResult(answer=FakeAnswer(row_count=1, warnings=[]))
    with pytest.raises(ValueError, match="contains"):
        verdict({"type": "warning"}, result)


def test_warning_fails_when_the_named_warning_is_absent():
    result = FakeResult(answer=FakeAnswer(row_count=1, warnings=["something else"]))
    ok, reason = verdict({"type": "warning", "contains": "incomplete"}, result)
    assert not ok, reason


# ---------------------------------------------------------------------------
# Empty reference data must never be read as agreement
# ---------------------------------------------------------------------------

def test_scalar_does_not_pass_when_both_sides_are_empty(monkeypatch):
    """close(None, None) is True, so an empty answer matched an empty oracle.

    This is a specification error rather than a failing question: an oracle
    that produces nothing cannot score anything, and reporting it as a system
    failure would blame the wrong component.
    """
    monkeypatch.setattr(runner, "reference", lambda sql, principal: [])
    result = FakeResult(answer=FakeAnswer(table=[], row_count=0))
    with pytest.raises(runner.SpecificationError, match="no value"):
        verdict({"type": "sql", "compare": "scalar", "sql": "SELECT 1"}, result)


def test_top_ids_does_not_pass_on_an_empty_oracle(monkeypatch):
    monkeypatch.setattr(runner, "reference", lambda sql, principal: [])
    result = FakeResult(answer=FakeAnswer(table=[], row_count=0))
    with pytest.raises(runner.SpecificationError, match="no ids"):
        verdict({"type": "sql", "compare": "top_ids", "sql": "SELECT 1"}, result)


def test_rows_does_not_pass_on_an_empty_oracle(monkeypatch):
    monkeypatch.setattr(runner, "reference", lambda sql, principal: [])
    result = FakeResult(answer=FakeAnswer(table=[{"dim0": "A", "value": 1}], row_count=1))
    with pytest.raises(runner.SpecificationError, match="no groups"):
        verdict({"type": "sql", "compare": "rows", "sql": "SELECT 1"}, result)


def test_rows_rejects_groups_the_oracle_did_not_produce(monkeypatch):
    """Checking only that expected groups exist ignores invented ones."""
    monkeypatch.setattr(
        runner, "reference", lambda sql, principal: [{"label": "A", "value": 10}])
    result = FakeResult(answer=FakeAnswer(
        table=[{"dim0": "A", "value": 10}, {"dim0": "GHOST", "value": 99}],
        row_count=2,
    ))
    ok, reason = verdict({"type": "sql", "compare": "rows", "sql": "SELECT 1"}, result)
    assert not ok, f"an extra group the oracle never produced was ignored: {reason}"


def test_top_ids_rejects_a_shorter_answer(monkeypatch):
    monkeypatch.setattr(
        runner, "reference",
        lambda sql, principal: [{"id": "a"}, {"id": "b"}, {"id": "c"}])
    result = FakeResult(answer=FakeAnswer(
        table=[{"dim0_id": "a"}, {"dim0_id": "b"}], row_count=2))
    ok, reason = verdict({"type": "sql", "compare": "top_ids", "sql": "SELECT 1"}, result)
    assert not ok, reason


# ---------------------------------------------------------------------------
# no_pricing must actually look
# ---------------------------------------------------------------------------

def test_no_pricing_fails_when_the_sql_was_never_captured():
    """With include_sql off, result.sql is None and the check was vacuous.

    Refusing to score is the right answer: the run was misconfigured, and
    certifying a statement nobody read would be worse than reporting nothing.
    """
    result = FakeResult(
        answer=FakeAnswer(headline="Paid pack units: 12 packs", row_count=1), sql=None)
    with pytest.raises(runner.SpecificationError, match="include_sql"):
        verdict({"type": "no_pricing"}, result)


def test_no_pricing_sees_currency_in_the_headline():
    result = FakeResult(
        answer=FakeAnswer(headline="Gross revenue: $250,766,926.42", row_count=1),
        sql="SELECT sum(pack_units) FROM sales",
    )
    ok, reason = verdict({"type": "no_pricing"}, result)
    assert not ok, f"currency in the headline was not noticed: {reason}"


def test_no_pricing_passes_a_genuinely_clean_response():
    result = FakeResult(
        answer=FakeAnswer(headline="Paid pack units: 12 packs",
                          table=[{"value_formatted": "12 packs"}], row_count=1),
        sql="SELECT sum(pack_units) FROM sales",
    )
    ok, reason = verdict({"type": "no_pricing"}, result)
    assert ok, reason


# ---------------------------------------------------------------------------
# denied
# ---------------------------------------------------------------------------

def test_denied_is_not_satisfied_by_an_error():
    result = FakeResult(status="error", message="something went wrong")
    ok, reason = verdict({"type": "denied"}, result)
    assert not ok, reason


# ---------------------------------------------------------------------------
# Unknown expectation types are a specification error, not a failing question
# ---------------------------------------------------------------------------

def test_an_unknown_expectation_type_raises():
    """Silently returning False turns a typo into a question that never passes."""
    with pytest.raises(ValueError, match="unknown expectation"):
        verdict({"type": "nonsense"}, FakeResult())


# ---------------------------------------------------------------------------
# Three more ways the judge could not fail
# ---------------------------------------------------------------------------

def test_a_currency_answer_is_not_a_volume_alternative():
    """It checked only whether the last column was labelled USD.

    A priced answer whose column header happened to say something else passed
    as "answered in volume, restriction disclosed" -- certifying the exact
    leak the expectation exists to catch.
    """
    result = FakeResult(
        answer=FakeAnswer(
            headline="Gross revenue: $250,766,926.42",
            columns=["account", "value"],
            table=[{"value_formatted": "$250,766,926.42"}],
            notes=["Pricing is restricted at your access level."],
            row_count=1,
        )
    )
    ok, reason = verdict({"type": "volume_alternative"}, result)
    assert not ok, f"currency accepted as a volume substitute: {reason}"


def test_a_volume_alternative_compiled_against_wac_is_refused():
    result = FakeResult(
        answer=FakeAnswer(headline="Paid pack units: 12 packs",
                          notes=["Pricing is restricted at your access level."],
                          row_count=1),
        sql="SELECT sum(s.wac) FROM sales s",
    )
    ok, reason = verdict({"type": "volume_alternative"}, result)
    assert not ok, reason


def test_a_genuine_volume_alternative_still_passes():
    result = FakeResult(
        answer=FakeAnswer(headline="Paid pack units: 388 packs",
                          columns=["value"],
                          table=[{"value_formatted": "388 packs"}],
                          notes=["Pricing is restricted at your access level, so this "
                                 "shows sales volume in packs rather than revenue."],
                          row_count=1),
        sql="SELECT sum(s.pack_units) FROM sales s",
    )
    ok, reason = verdict({"type": "volume_alternative"}, result)
    assert ok, reason


def test_an_execution_error_is_not_a_passing_no_pricing_case():
    """A crash discloses no pricing the way a request never sent discloses
    none. Counting it as a success turns every failure into evidence of
    security."""
    result = FakeResult(status="error", answer=None,
                        sql="SELECT sum(s.pack_units) FROM sales s")
    ok, reason = verdict({"type": "no_pricing"}, result)
    assert not ok, f"an execution error passed as no_pricing: {reason}"


def test_an_unrelated_cohort_is_not_a_frozen_cohort():
    """"Frozen" means THESE accounts, not some accounts.

    Checking only that the list was non-empty accepted an entirely different
    population as a correctly preserved cohort.
    """
    previous = FakeResult(answer=FakeAnswer(
        table=[{"dim0_id": "ORG-1"}, {"dim0_id": "ORG-2"}, {"dim0_id": "ORG-3"}],
        row_count=3))
    result = FakeResult(plan={"filters": {"account_ids": ["ORG-9", "ORG-8", "ORG-7"]}})
    ok, reason = runner.judge_turn({"type": "frozen_cohort"}, result, previous)
    assert not ok, f"an unrelated cohort passed as frozen: {reason}"


def test_the_actual_previous_cohort_is_a_frozen_cohort():
    previous = FakeResult(answer=FakeAnswer(
        table=[{"dim0_id": "ORG-1"}, {"dim0_id": "ORG-2"}], row_count=2))
    result = FakeResult(plan={"filters": {"account_ids": ["ORG-2", "ORG-1"]}})
    ok, reason = runner.judge_turn({"type": "frozen_cohort"}, result, previous)
    assert ok, reason


def test_a_partially_carried_cohort_is_refused():
    previous = FakeResult(answer=FakeAnswer(
        table=[{"dim0_id": "ORG-1"}, {"dim0_id": "ORG-2"}, {"dim0_id": "ORG-3"}],
        row_count=3))
    result = FakeResult(plan={"filters": {"account_ids": ["ORG-1", "ORG-2"]}})
    ok, reason = runner.judge_turn({"type": "frozen_cohort"}, result, previous)
    assert not ok, reason
