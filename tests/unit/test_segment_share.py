"""Generic share is a share OF THE MARKET, not our share of it.

"What is the generic share in the Platinum Compounds market?" was answered
with brand_market_share -- the company's own share, 71.79%. That is close to
the opposite of the question: the questioner wants to know how much of the
market has gone generic, and got how much of it is ours.

The fix is a metric, not a caveat. market_segment_share divides the filtered
segment's reported market volume by the whole market's, so the same metric
answers generic share, biosimilar share and branded-competitor share depending
on the classification filter.
"""

from __future__ import annotations

import pytest

from app.analytics.plan import AnalyticalPlan, MetricKey
from app.analytics.registry import get_registry
from app.llm.planner import OfflinePlanner, PlanningContext

ANCHOR = {
    "min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
    "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3",
}


def planned(question: str):
    context = PlanningContext(
        role="exec", scope_description="all territories and regions",
        wac_authorized=True, reporting_anchor=ANCHOR,
        known_products=["ZENOVAX"], known_categories=["Platinum Compounds"],
    )
    return OfflinePlanner().plan(question, context)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_the_segment_share_metric_exists_and_is_a_ratio():
    spec = get_registry().get("market_segment_share")
    assert spec["kind"] == "ratio"
    assert spec["numerator"] == spec["denominator"] == "market_equivalents"


def test_its_denominator_is_the_surrounding_market():
    """Same source on both sides; the segment filter is what separates them.
    If the denominator kept the filter, every answer would be 100%."""
    spec = get_registry().get("market_segment_share")
    assert spec["denominator_population"] == "surrounding_market"


def test_it_declares_that_the_classification_is_derived():
    spec = get_registry().get("market_segment_share")
    assert "derived_classification" in spec["quality_checks"]


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,segment", [
    ("What is the generic share in the Platinum Compounds market?", "generic"),
    ("What is the biosimilar share of this market?", "biosimilar"),
    ("What is the competitor share in Platinum Compounds?", "branded_competitor"),
])
def test_a_segment_question_picks_the_segment_metric_and_sets_the_filter(
    question, segment
):
    plan = planned(question)
    assert plan.metric == MetricKey.market_segment_share, question
    assert plan.filters.classifications == [segment], question


def test_our_own_market_share_is_still_brand_market_share():
    """The fix must not capture the question it was not for."""
    plan = planned("What is our market share for Zenovax?")
    assert plan.metric == MetricKey.brand_market_share
    assert plan.filters.classifications == []


def test_a_share_trend_question_still_resolves_to_the_trend_metric():
    plan = planned("Which products gained the most market share versus the prior quarter?")
    assert plan.metric == MetricKey.share_trend_pp


# ---------------------------------------------------------------------------
# The compiled shape
# ---------------------------------------------------------------------------

def test_the_denominator_drops_the_segment_filter_and_the_numerator_keeps_it():
    from app.analytics.compiler import Compiler

    plan = AnalyticalPlan.model_validate({
        "metric": "market_segment_share",
        "filters": {"market_categories": ["Platinum Compounds"],
                    "classifications": ["generic"]},
        "time": {"kind": "named", "named": "r3m"},
    })
    query = Compiler().compile(plan, anchor=ANCHOR)
    numerator = query.sql[: query.sql.index("den AS")]
    denominator = query.sql[query.sql.index("den AS"):]
    assert "classification" in numerator, "the numerator lost the segment filter"
    assert "classification" not in denominator, (
        "the denominator kept the segment filter, so the share is always 100%"
    )


# ---------------------------------------------------------------------------
# 340B share of volume
# ---------------------------------------------------------------------------

def test_the_340b_share_metric_exists_and_ignores_340b_in_its_denominator():
    """"What percentage of volume comes from 340B accounts" is a ratio.

    It was answered with 59,419 packs -- a count, for a question asking for a
    percentage -- because no metric expressed "a filtered subset over the
    unfiltered whole".
    """
    spec = get_registry().get("share_340b")
    assert spec["kind"] == "ratio"
    assert spec["numerator"] == spec["denominator"] == "paid_pack_units"
    assert spec["denominator_population"] == "ignores_340b"


def test_a_340b_percentage_question_picks_the_ratio_not_a_count():
    plan = planned("What percentage of our volume comes from 340B accounts this quarter?")
    assert plan.metric == MetricKey.share_340b
    assert plan.filters.is_340b.value == "only"


def test_excluding_340b_is_still_a_plain_volume_question():
    """A bare exclusion is not a proportion, and must not become one."""
    plan = planned("Show me volume excluding 340B accounts this quarter")
    assert plan.metric == MetricKey.paid_pack_units
    assert plan.filters.is_340b.value == "exclude"


def test_the_denominator_lifts_only_the_340b_condition():
    """Every other filter stays on both sides, so "what share of our Zenovax
    volume is 340B" divides by Zenovax volume, not by everything."""
    from app.analytics.compiler import Compiler

    plan = AnalyticalPlan.model_validate({
        "metric": "share_340b",
        "filters": {"is_340b": "only", "product_names": ["ZENOVAX"]},
        "time": {"kind": "named", "named": "r3m"},
    })
    sql = Compiler().compile(plan, anchor=ANCHOR).sql
    numerator = sql[: sql.index("den AS")]
    denominator = sql[sql.index("den AS"):]
    assert "is_340b" in numerator, "the numerator lost the 340B condition"
    assert "is_340b" not in denominator, "the denominator kept it, so the share is 100%"
    assert "drug_name" in denominator, "the denominator dropped the product filter too"


def test_share_340b_is_correct_even_when_the_planner_sets_no_340b_filter():
    """The metric defines both sides; it does not trust the planner for either.

    Found on the deployed instance. The live model chose share_340b and left
    is_340b at "include", so the numerator was identical to the denominator
    and the answer was a confident 100%. The offline planner sets "only", so
    only a real model exposed it.
    """
    from app.analytics.compiler import Compiler

    for requested in ("include", "only"):
        plan = AnalyticalPlan.model_validate({
            "metric": "share_340b",
            "filters": {"is_340b": requested},
            "time": {"kind": "named", "named": "r3m"},
        })
        sql = Compiler().compile(plan, anchor=ANCHOR).sql
        numerator = sql[: sql.index("den AS")]
        denominator = sql[sql.index("den AS"):]
        assert "is_340b" in numerator, f"numerator lost the 340B condition ({requested})"
        assert "is_340b" not in denominator, (
            f"denominator kept the 340B condition ({requested}); the share is 100%"
        )
