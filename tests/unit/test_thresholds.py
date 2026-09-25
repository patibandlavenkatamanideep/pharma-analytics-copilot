"""A threshold is the question, not a decoration on it.

"Which accounts have declined more than 20%" answered with an unfiltered
growth ranking answers a different question. The threshold is now part of the
typed plan, compared against the plan's own metric in that metric's units --
a separate threshold metric would be a disclosure channel, since filtering by
revenue leaks revenue even when the column is hidden.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan
from app.llm.planner import OfflinePlanner, PlanningContext

ANCHOR = {"min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}


def planned(question):
    return OfflinePlanner().plan(question, PlanningContext(
        role="exec", scope_description="all", wac_authorized=True,
        reporting_anchor=ANCHOR, known_products=["ZENOVAX"]))


# ---------------------------------------------------------------------------
# Reading the threshold out of the question
# ---------------------------------------------------------------------------

def test_a_percentage_decline_is_a_negative_ratio_bound():
    """"declined more than 20%" is a fall past -20%, not a rise past 20%."""
    t = planned("Which accounts have declined more than 20% in volume vs prior quarter?").threshold
    assert t is not None
    assert t.direction == "below" and t.value == pytest.approx(-0.20)


def test_a_volume_threshold_keeps_the_metric_units():
    t = planned("Which accounts bought more than 500 packs this quarter?").threshold
    assert t.direction == "above" and t.value == pytest.approx(500)


@pytest.mark.parametrize("phrase,direction", [
    ("more than 100 packs", "above"), ("at least 100 packs", "above"),
    ("over 100 packs", "above"), ("fewer than 100 packs", "below"),
    ("less than 100 packs", "below"), ("under 100 packs", "below"),
])
def test_both_directions_are_read(phrase, direction):
    t = planned(f"Which accounts bought {phrase} this quarter?").threshold
    assert t is not None and t.direction == direction


def test_a_question_with_no_threshold_gets_none():
    assert planned("What are our top 5 accounts by pack units this quarter?").threshold is None


def test_a_threshold_without_a_breakdown_is_rejected():
    """Filtering a single total either returns it or returns nothing."""
    with pytest.raises(ValueError, match="requires at least one dimension"):
        AnalyticalPlan.model_validate({
            "metric": "paid_pack_units", "dimensions": [],
            "threshold": {"direction": "above", "value": 500},
            "time": {"kind": "named", "named": "r3m"}})


# ---------------------------------------------------------------------------
# Compiling it
# ---------------------------------------------------------------------------

def compiled(**plan_bits):
    plan = AnalyticalPlan.model_validate({
        "dimensions": ["account"], "time": {"kind": "named", "named": "r3m"},
        **plan_bits})
    return Compiler(max_rows=50).compile(plan, anchor=ANCHOR)


def planned_dims(question):
    return planned(question)


def test_the_threshold_is_a_bind_parameter_not_interpolated():
    q = compiled(metric="paid_pack_units",
                 threshold={"direction": "above", "value": 500})
    assert "500" not in q.sql
    assert 500.0 in q.params


def test_the_filter_runs_before_the_ranking():
    """A cap applied first would rank the wrong population."""
    q = compiled(metric="paid_pack_units",
                 threshold={"direction": "above", "value": 500},
                 ranking={"direction": "bottom", "limit": 5})
    where_at = q.sql.rindex("WHERE value")
    limit_at = q.sql.rindex("LIMIT")
    assert where_at < limit_at, "the row cap was applied before the threshold"


def test_nulls_are_excluded_rather_than_compared():
    """A null growth figure is not "below -20%"; it is unknown."""
    q = compiled(metric="paid_pack_units",
                 threshold={"direction": "below", "value": 10})
    assert "IS NOT NULL" in q.sql


@pytest.mark.parametrize("metric,extra", [
    ("paid_pack_units", {}),
    ("brand_market_share", {}),
    ("volume_growth", {"comparison": {"kind": "named", "named": "r6m_prior"}}),
])
def test_every_query_shape_accepts_a_threshold(metric, extra):
    """Simple aggregate, ratio and period comparison each compute the value
    differently; the threshold wraps all three the same way."""
    q = compiled(metric=metric, threshold={"direction": "above", "value": 1}, **extra)
    assert "AS filtered" in q.sql


def test_no_threshold_leaves_the_query_unwrapped():
    q = compiled(metric="paid_pack_units")
    assert "AS filtered" not in q.sql


# ---------------------------------------------------------------------------
# Rolling averages
# ---------------------------------------------------------------------------

def test_a_rolling_average_is_read_from_the_question():
    plan = planned("Show me the rolling 3-month average volume for Zenovax")
    assert plan.rolling is not None and plan.rolling.periods == 3


def test_a_moving_average_implies_a_period_grain():
    """It is an average over time, so it needs a series even when the question
    does not say "by month"."""
    from app.analytics.plan import Dimension

    plan = planned("What is the 6-month moving average of our volume?")
    assert plan.rolling.periods == 6
    assert Dimension.period_mo in plan.dimensions


def test_the_window_is_widened_so_the_average_has_something_to_average():
    """A 3-month average over a 3-month window is two-thirds leading edge."""
    plan = planned("Show me the rolling 3-month average volume for Zenovax")
    assert plan.time.named.value != "r3m"


def test_an_explicit_window_is_respected():
    plan = planned("Show me the rolling 3-month average volume year to date")
    assert plan.time.named.value == "ytd"


def test_a_plain_trend_is_not_a_rolling_average():
    assert planned(
        "Show me the monthly volume trend over the last six months").rolling is None


def test_a_rolling_average_needs_exactly_one_period_dimension():
    with pytest.raises(ValueError, match="exactly one period dimension"):
        AnalyticalPlan.model_validate({
            "metric": "paid_pack_units", "dimensions": ["account"],
            "rolling": {"periods": 3},
            "time": {"kind": "named", "named": "last_6_months"}})


def test_a_rolling_average_cannot_also_be_a_two_window_comparison():
    with pytest.raises(ValueError, match="cannot be combined"):
        AnalyticalPlan.model_validate({
            "metric": "volume_growth", "dimensions": ["period_mo"],
            "rolling": {"periods": 3},
            "time": {"kind": "named", "named": "r3m"},
            "comparison": {"kind": "named", "named": "r6m_prior"}})


def test_the_compiled_window_averages_the_right_number_of_periods():
    q = compiled(metric="paid_pack_units", dimensions=["period_mo"],
                 rolling={"periods": 3},
                 time={"kind": "named", "named": "last_6_months"})
    assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in q.sql
    assert "ORDER BY dim0_id" in q.sql
    # The un-averaged point is kept beside the average.
    assert "point_value" in q.columns


def test_a_rolling_average_partitions_by_the_other_dimension():
    """Averaging across products would mix unrelated series together."""
    q = compiled(metric="paid_pack_units", dimensions=["product", "period_mo"],
                 rolling={"periods": 3},
                 time={"kind": "named", "named": "last_6_months"})
    assert "PARTITION BY dim0_id" in q.sql
