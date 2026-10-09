"""What a threshold phrase means, and that the plan keeps it.

Review finding 5b: "at least 100 packs" compiled to ``> 100``, so an account
that bought exactly 100 disappeared from an answer that asked for it. The
plan could only say ``above`` or ``below``, and both were strict.

The operator is now explicit and one table decides what each phrase means,
shared by the offline planner, the live prompt and the intent guard. These
tests pin the table, the units rule, the reading of plans stored before the
change, and the guard that catches a plan which lost the bound on the way.
"""

from __future__ import annotations

import pytest

from app.analytics import intent
from app.analytics.compiler import Compiler
from app.analytics.entities import Vocabulary
from app.analytics.plan import AnalyticalPlan, Threshold
from app.analytics.thresholds import (
    SQL_OPERATOR,
    expected_value,
    prompt_guidance,
    read_threshold,
)

ANCHOR = {"min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}
R3M = {"kind": "named", "named": "r3m"}


# ---------------------------------------------------------------------------
# The phrase table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,op", [
    ("more than 100", "gt"), ("greater than 100", "gt"), ("over 100", "gt"),
    ("above 100", "gt"), ("exceeding 100", "gt"), ("in excess of 100", "gt"),
    ("at least 100", "gte"), ("no less than 100", "gte"),
    ("no fewer than 100", "gte"), ("a minimum of 100", "gte"),
    ("100 or more", "gte"), ("100 or greater", "gte"),
    ("less than 100", "lt"), ("fewer than 100", "lt"), ("under 100", "lt"),
    ("below 100", "lt"),
    ("at most 100", "lte"), ("no more than 100", "lte"),
    ("not more than 100", "lte"), ("up to 100", "lte"),
    ("100 or fewer", "lte"), ("100 or less", "lte"),
])
def test_each_phrase_reads_as_its_operator(phrase, op):
    read = read_threshold(f"Which accounts bought {phrase} packs?")
    assert read is not None and read.op == op and read.number == 100


def test_the_earliest_phrase_wins_so_no_more_than_is_not_more_than():
    """'no more than 5' contains 'more than 5'. Read as the inner phrase it
    would flip the bound to the opposite side."""
    assert read_threshold("accounts with no more than 5 packs").op == "lte"
    assert read_threshold("accounts with not less than 5 packs").op == "gte"


@pytest.mark.parametrize("question", [
    "What was volume over 3 months?",
    "What was volume over 30 months?",           # must not backtrack to "over 3"
    "Show volume under 6 weeks of data",
    "Top 10 accounts by volume",
    "Volume for the last quarter",
])
def test_a_time_span_or_a_ranking_is_not_a_threshold(question):
    assert read_threshold(question) is None


def test_thousands_separators_are_read():
    assert read_threshold("more than 1,250 packs").number == 1250


def test_a_decline_flips_to_an_upper_bound_on_growth():
    """'declined more than 20%' is growth below -20%."""
    read = read_threshold("accounts that declined more than 20%")
    assert read.op == "lt" and read.decline
    read = read_threshold("accounts that declined at least 20%")
    assert read.op == "lte"


def test_a_small_decline_is_a_range_not_a_bound():
    """'declined less than 20%' is -0.2 < growth < 0. One bound (> -0.2)
    would include every account that GREW, labelled as small decliners."""
    read = read_threshold("accounts that declined less than 20%")
    assert read.needs_range and read.op is None


# ---------------------------------------------------------------------------
# Units, from the registry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,unit,want", [
    ("more than 20%", "ratio", 0.2),
    ("more than 20", "ratio", 0.2),                 # a bare number on a share
    ("more than 2 points", "percentage points", 2),
    ("more than 500 packs", "packs", 500),
    ("more than 500", "USD", 500),
    ("declined more than 20%", "ratio", -0.2),
])
def test_the_bound_is_expressed_in_the_metrics_own_units(text, unit, want):
    assert expected_value(read_threshold(text), unit) == pytest.approx(want)


def test_a_percentage_of_a_count_is_not_a_bound_on_the_count():
    """'more than 20%' of packs has no meaning as a pack threshold. The old
    planner divided by 100 regardless and filtered on 0.2 packs."""
    assert expected_value(read_threshold("more than 20% packs"), "packs") is None


# ---------------------------------------------------------------------------
# Compiling, and plans stored before the operator existed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op,sql", sorted(SQL_OPERATOR.items()))
def test_each_operator_compiles_to_its_comparison(op, sql):
    plan = AnalyticalPlan.model_validate({
        "metric": "paid_pack_units", "dimensions": ["account"], "time": R3M,
        "threshold": {"op": op, "value": 100}})
    q = Compiler(max_rows=50).compile(plan, anchor=ANCHOR)
    assert f"value {sql} %s" in q.sql


@pytest.mark.parametrize("legacy,op", [("above", "gt"), ("below", "lt")])
def test_a_stored_plan_keeps_its_original_strict_meaning(legacy, op):
    """A follow-up patches the previous turn's stored plan. Plans written
    before the change said above/below and always compiled strictly; reading
    them as anything else would move the boundary of the answer being
    followed up."""
    t = Threshold.model_validate({"direction": legacy, "value": 5})
    assert t.op == op


def test_an_unknown_legacy_direction_is_rejected_not_guessed():
    with pytest.raises(ValueError):
        Threshold.model_validate({"direction": "sideways", "value": 5})


def test_a_stored_plan_round_trips_through_the_full_plan_model():
    plan = AnalyticalPlan.model_validate({
        "metric": "paid_pack_units", "dimensions": ["account"], "time": R3M,
        "threshold": {"direction": "above", "value": 500}})
    assert plan.model_dump(mode="json")["threshold"] == {"op": "gt", "value": 500.0}


# ---------------------------------------------------------------------------
# The guard: did the bound reach the plan intact?
# ---------------------------------------------------------------------------

VOCAB = Vocabulary(products=["ZENOVAX"])


def gaps(question, **threshold_plan):
    plan = AnalyticalPlan.model_validate({
        "metric": threshold_plan.pop("metric", "paid_pack_units"),
        "dimensions": ["account"], "time": R3M, **threshold_plan})
    return intent.find_gaps(question, plan, VOCAB)


def kinds(found):
    return {g.kind for g in found}


def test_a_faithful_plan_raises_nothing():
    assert not kinds(gaps("accounts with at least 100 packs",
                          threshold={"op": "gte", "value": 100})) & {
        "threshold_direction_mismatch", "threshold_value_mismatch",
        "threshold_boundary", "unsupported_threshold"}


def test_the_opposite_direction_is_blocking():
    found = gaps("accounts with more than 100 packs", threshold={"op": "lt", "value": 100})
    assert "threshold_direction_mismatch" in kinds(intent.blocking(found))


def test_a_share_bound_in_the_wrong_units_is_blocking():
    """20 against a share is 2,000% and returns nothing; the question meant
    0.2."""
    found = gaps("accounts with share above 20%", metric="brand_market_share",
                 threshold={"op": "gt", "value": 20})
    assert "threshold_value_mismatch" in kinds(intent.blocking(found))


def test_a_lost_boundary_is_disclosed_with_the_rows_it_drops():
    """Only rows exactly at the bound differ, so the answer is otherwise
    right -- but the note must say which rows are missing."""
    found = gaps("accounts with at least 100 packs", threshold={"op": "gt", "value": 100})
    boundary = [g for g in found if g.kind == "threshold_boundary"]
    assert boundary and "not included" in boundary[0].detail
    assert not intent.blocking(found)


def test_a_dropped_threshold_is_disclosed():
    found = gaps("accounts with at least 100 packs")
    assert "unsupported_threshold" in kinds(found)


def test_a_range_is_disclosed_rather_than_approximated():
    found = gaps("accounts that declined less than 20%", metric="volume_growth",
                 comparison={"kind": "named", "named": "r6m_prior"})
    assert any("range" in g.detail for g in found if g.kind == "unsupported_threshold")


# ---------------------------------------------------------------------------
# One table, three consumers
# ---------------------------------------------------------------------------

def test_the_live_prompt_is_generated_from_the_table():
    text = prompt_guidance()
    for op in ("gt", "gte", "lt", "lte"):
        assert f"op {op}" in text
    assert "'at least N'" in text and "'no more than N'" in text


@pytest.mark.parametrize("question", [
    "Which accounts bought at least 100 packs this quarter?",
    "Which accounts bought more than 100 packs this quarter?",
    "Which accounts bought no more than 100 packs this quarter?",
    "Which accounts bought 100 or fewer packs this quarter?",
    "Which accounts have declined more than 20% vs the prior quarter?",
    "Which accounts have declined at least 20% vs the prior quarter?",
])
def test_the_offline_planner_and_the_guard_agree(question):
    """A planner and its checker built from different tables would raise a
    mismatch on the planner's own output."""
    from app.llm.planner import OfflinePlanner, PlanningContext
    plan = OfflinePlanner().plan(question, PlanningContext(
        role="exec", scope_description="all", wac_authorized=True,
        reporting_anchor=ANCHOR, known_products=["ZENOVAX"])).plan
    assert plan.threshold is not None
    found = intent.find_gaps(question, plan, VOCAB)
    assert not kinds(found) & {"threshold_direction_mismatch",
                               "threshold_value_mismatch", "threshold_boundary"}
