"""Two ways a confident number was wrong.

A therapeutic area the plan never applied, and a comparison whose two sides
could never be joined.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import CompileError, Compiler
from app.analytics.entities import Vocabulary
from app.analytics.intent import blocking, find_gaps
from app.analytics.plan import AnalyticalPlan
from app.llm.planner import OfflinePlanner, PlanningContext

ANCHOR = {"min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}

VOCAB = Vocabulary(products=["ZENOVAX"], specialties=["Oncology", "Urology"],
                   all_territories=["Mid-Atlantic West"])


def planned(question):
    return OfflinePlanner().plan(question, PlanningContext(
        role="exec", scope_description="all", wac_authorized=True,
        reporting_anchor=ANCHOR, known_products=["ZENOVAX"],
        known_specialties=["Oncology", "Urology"]))


# ---------------------------------------------------------------------------
# Therapeutic area
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("How is our oncology portfolio performing overall in pack units?", "Oncology"),
    ("What is our total urology volume this quarter?", "Urology"),
])
def test_a_named_therapeutic_area_becomes_a_filter(question, expected):
    """It was absent from the vocabulary, so neither planner knew it existed
    and "our oncology portfolio" returned every product's volume."""
    assert planned(question).filters.specialties == [expected]


def test_an_unqualified_question_sets_no_specialty():
    assert planned("What are our total pack units this quarter?").filters.specialties == []


def test_a_dropped_therapeutic_area_blocks_rather_than_answering_broadly():
    """A wrong number, not an incomplete one -- so it must not be answered."""
    plan = AnalyticalPlan.model_validate({
        "metric": "paid_pack_units", "filters": {},
        "time": {"kind": "named", "named": "r3m"}})
    gaps = find_gaps("How is our oncology portfolio performing?", plan, VOCAB)
    assert "unhonoured_specialty" in {g.kind for g in gaps}
    assert blocking(gaps)


def test_an_honoured_therapeutic_area_is_not_flagged():
    plan = AnalyticalPlan.model_validate({
        "metric": "paid_pack_units", "filters": {"specialties": ["Oncology"]},
        "time": {"kind": "named", "named": "r3m"}})
    assert find_gaps("How is our oncology portfolio performing?", plan, VOCAB) == []


# ---------------------------------------------------------------------------
# Period comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grain", ["period_qtr", "period_mo", "period_wk"])
def test_a_period_grain_inside_a_comparison_is_refused_by_name(grain):
    """The two sides carry different period labels, so the join matches
    nothing and every growth figure comes back null -- which reads as "no
    growth data" rather than as an unsupported question."""
    plan = AnalyticalPlan.model_validate({
        "metric": "volume_growth", "dimensions": [grain],
        "time": {"kind": "named", "named": "r3m"},
        "comparison": {"kind": "named", "named": "r6m_prior"}})
    with pytest.raises(CompileError) as exc:
        Compiler().compile(plan, anchor=ANCHOR)
    message = str(exc.value)
    assert "cannot also be a two-window comparison" in message
    # It must say what DOES work, or it is just a dead end.
    assert "trend" in message and "one growth figure" in message


@pytest.mark.parametrize("dims", [[], ["account"], ["product"]])
def test_comparisons_on_non_period_grains_still_compile(dims):
    plan = AnalyticalPlan.model_validate({
        "metric": "volume_growth", "dimensions": dims,
        "time": {"kind": "named", "named": "r3m"},
        "comparison": {"kind": "named", "named": "r6m_prior"}})
    assert Compiler().compile(plan, anchor=ANCHOR).sql


def test_a_period_series_without_a_comparison_still_compiles():
    """The suggested alternative has to actually work."""
    plan = AnalyticalPlan.model_validate({
        "metric": "paid_pack_units", "dimensions": ["period_qtr"],
        "time": {"kind": "named", "named": "last_6_months"}})
    assert Compiler().compile(plan, anchor=ANCHOR).sql
