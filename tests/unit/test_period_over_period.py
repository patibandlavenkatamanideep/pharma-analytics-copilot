"""Each period against the one before it: a supported question, distinct from
a series, a two-window comparison and a rolling average.

k-07 (evals/holdout2.yaml), one of the supplied sample questions
(docs/product_analytics.md, "Trend & Growth"): "Is Zenovax volume growing or
declining month over month?" The offline planner read "growing or declining"
as the two-window metric volume_growth and "month over month" as a monthly
breakdown -- a combination the compiler refuses -- so the question came back as
a clarification, and the clarification itself said "a mo-by-mo breakdown". No
plan could express growth of each period against the previous one.

The first group reproduced it on the unmodified code
(evidence/runs/r5-k07-reproduced.json). No database here; the numbers are
checked against an independent oracle in
tests/integration/test_period_over_period_fixture.py.
"""

from __future__ import annotations

import pytest

from app.analytics.plan import AnalyticalPlan, Dimension, MetricKey
from app.llm.planner import OfflinePlanner, PlanningContext

K07 = pytest.mark.xfail(strict=True, reason="k-07: per-period change is not expressible")

ANCHOR = {"min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}


def plan_for(question: str, wac: bool = True) -> AnalyticalPlan:
    context = PlanningContext(role="exec", scope_description="all", wac_authorized=wac,
                              reporting_anchor=ANCHOR, known_products=["ZENOVAX", "PAXELIUM"])
    return OfflinePlanner().plan(question, context).plan


@K07
@pytest.mark.parametrize("question", [
    "Is Zenovax volume growing or declining month over month?",
    "How is Zenovax volume changing month over month?",
    "Zenovax month-over-month growth in pack units",
    "Is Zenovax volume going up or down each month compared with the month before?",
    "Zenovax MoM change",
])
def test_month_over_month_is_a_monthly_series_with_each_months_change(question):
    plan = plan_for(question)
    assert plan.clarification is None, plan.clarification
    assert plan.metric is MetricKey.paid_pack_units
    assert plan.dimensions == [Dimension.period_mo]
    assert plan.period_over_period is True
    assert plan.comparison is None


@K07
@pytest.mark.parametrize("question,grain", [
    ("Is Zenovax growing quarter over quarter?", Dimension.period_qtr),
    ("Zenovax pack units week over week", Dimension.period_wk),
])
def test_other_grains_take_the_same_shape(question, grain):
    plan = plan_for(question)
    assert (plan.dimensions, plan.period_over_period) == ([grain], True)


@K07
def test_explicit_units_are_kept():
    plan = plan_for("Is Zenovax growing month over month in equivalents?")
    assert (plan.metric, plan.period_over_period) == (MetricKey.paid_equivalents, True)


@K07
def test_a_plain_series_and_a_two_window_comparison_stay_what_they_were():
    series = plan_for("Show me Zenovax volume by month")
    assert (series.dimensions, series.period_over_period) == ([Dimension.period_mo], False)
    growth = plan_for("What is Zenovax volume growth this quarter versus last quarter?")
    assert growth.metric is MetricKey.volume_growth and growth.comparison is not None
    assert growth.period_over_period is False
    rolling = plan_for("What is the rolling 3-month average volume for Paxelium?")
    assert rolling.rolling is not None and rolling.period_over_period is False


@K07
@pytest.mark.parametrize("extra,why", [
    ({"dimensions": []}, "exactly one period dimension"),
    ({"dimensions": ["product"]}, "exactly one period dimension"),
    ({"dimensions": ["period_mo", "period_qtr"]}, "exactly one period dimension"),
    ({"comparison": {"kind": "named", "named": "last_quarter"}, "metric": "volume_growth"},
     "two-window comparison"),
    ({"rolling": {"periods": 3}}, "rolling average"),
    ({"ranking": {"direction": "top", "limit": 3}}, "ranking"),
])
def test_an_incoherent_period_over_period_plan_is_refused(extra, why):
    plan = {"metric": "paid_pack_units", "dimensions": ["period_mo"],
            "time": {"kind": "named", "named": "r3m"}, "period_over_period": True, **extra}
    with pytest.raises(ValueError, match=why):
        AnalyticalPlan.model_validate(plan)


@K07
@pytest.mark.parametrize("metric", ["brand_market_share", "facility_count_all"])
def test_a_metric_without_an_additive_series_is_refused_by_name(metric):
    from app.analytics.compiler import check_compatibility
    from app.analytics.registry import get_registry

    plan = AnalyticalPlan.model_validate({
        "metric": metric, "dimensions": ["period_mo"],
        "time": {"kind": "named", "named": "r3m"}, "period_over_period": True})
    reasons = check_compatibility(plan, get_registry().get(metric))
    assert any("period over period" in r.lower() or "each period against" in r.lower()
               for r in reasons), reasons


@K07
def test_the_refusal_of_growth_by_month_names_the_month():
    """The clarification for a two-window metric broken down by period said
    "A mo-by-mo breakdown" and "volume by mo": the grain's code, not a word."""
    from app.analytics.compiler import check_compatibility
    from app.analytics.registry import get_registry

    plan = AnalyticalPlan.model_validate({
        "metric": "volume_growth", "dimensions": ["period_mo"],
        "time": {"kind": "named", "named": "r3m"},
        "comparison": {"kind": "named", "named": "last_quarter"}})
    text = " ".join(check_compatibility(plan, get_registry().get("volume_growth")))
    assert "month-by-month" in text and "mo-by-mo" not in text and "by mo\"" not in text
