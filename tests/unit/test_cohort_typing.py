"""A cohort is a set of ids AT A GRAIN, and the grain travels with them.

"Show me those same ones by month" freezes the previous population. Stored
without its grain, a cohort is just a list of strings, and it was applied to
account_ids whatever it actually held: a cohort of products became a list of
organization ids, matched nothing, and the follow-up returned a different
population while looking like it had worked.
"""

from __future__ import annotations

import pytest

from app.llm.planner import COHORT_FILTER_FIELD, OfflinePlanner, PlanningContext

ANCHOR = {
    "min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
    "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3",
}


def plan_follow_up(dimension, cohort, question="Show me those same ones by month"):
    context = PlanningContext(
        role="exec", scope_description="all territories and regions",
        wac_authorized=True, reporting_anchor=ANCHOR,
        known_products=["ZENOVAX", "GEMTARA"],
        previous_cohort=list(cohort), previous_cohort_dimension=dimension,
    )
    return OfflinePlanner().plan(question, context)


def test_a_product_cohort_is_not_applied_as_account_ids():
    plan = plan_follow_up("product", ["ZENOVAX", "GEMTARA"])
    assert plan.filters.account_ids == [], "product names were used as organization ids"
    assert plan.filters.product_names == ["ZENOVAX", "GEMTARA"]


def test_an_account_cohort_still_goes_to_account_ids():
    plan = plan_follow_up("account", ["ORG-1", "ORG-2"])
    assert plan.filters.account_ids == ["ORG-1", "ORG-2"]
    assert plan.filters.product_names == []


@pytest.mark.parametrize("dimension,target", sorted(COHORT_FILTER_FIELD.items()))
def test_every_supported_grain_lands_in_its_own_filter(dimension, target):
    plan = plan_follow_up(dimension, ["A", "B"])
    assert getattr(plan.filters, target) == ["A", "B"]
    others = set(COHORT_FILTER_FIELD.values()) - {target}
    for field_name in others:
        assert getattr(plan.filters, field_name) == [], f"{dimension} leaked into {field_name}"


def test_a_period_cohort_is_not_a_population():
    """"Those same months" is a time window, not a set of entities."""
    plan = plan_follow_up("period_mo", ["2026-08", "2026-09"])
    for field_name in COHORT_FILTER_FIELD.values():
        assert getattr(plan.filters, field_name) == []


def test_an_untyped_cohort_is_not_guessed_at():
    """Older turns have no recorded grain. Applying them anywhere is a guess."""
    plan = plan_follow_up(None, ["ORG-1", "ORG-2"])
    for field_name in COHORT_FILTER_FIELD.values():
        assert getattr(plan.filters, field_name) == []


def test_a_fresh_question_does_not_inherit_the_cohort():
    plan = plan_follow_up("account", ["ORG-1"],
                          question="What are our total pack units this quarter?")
    assert plan.filters.account_ids == []
