"""A cohort is a set of ids AT A GRAIN, and the grain travels with them.

"Show me those same ones by month" freezes the previous population. Stored
without its grain, a cohort is just a list of strings, and it was applied to
account_ids whatever it actually held: a cohort of products became a list of
organization ids, matched nothing, and the follow-up returned a different
population while looking like it had worked.

The mechanism changed on 30 September. The planners used to copy the ids
into the typed filter for the grain, and the plan's account filter holds at
most 200 -- so a 500-account cohort could not be frozen whole. The server now
binds the stored population to the query itself (CohortBinding), through the
same SQL expression the typed filter for that grain uses. These tests keep
the original guarantee -- a cohort is applied at its own grain and no other
-- and pin it against the new mechanism.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import CohortBinding, Compiler
from app.analytics.plan import AnalyticalPlan
from app.llm.planner import COHORT_FILTER_FIELD, OfflinePlanner, PlanningContext

ANCHOR = {
    "min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
    "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3",
}
R3M = {"kind": "named", "named": "r3m"}

#: The expression each grain's cohort must be applied through -- the same
#: one its typed filter uses.
EXPRESSION = {
    "account": "COALESCE(o.grandparent_org_id, o.org_id) = ANY(%s)",
    "facility": "o.org_id = ANY(%s)",
    "gpo": "o.gpo_name = ANY(%s)",
    "archetype": "o.org_archetype = ANY(%s)",
    "territory": "z.territory_name = ANY(%s)",
    "region": "z.region_name = ANY(%s)",
    "product": "upper(p.drug_name) = ANY(%s)",
}


def compiled(dimension, ids, metric="paid_pack_units"):
    plan = AnalyticalPlan.model_validate({"metric": metric, "time": R3M})
    return Compiler(max_rows=50).compile(
        plan, anchor=ANCHOR, cohort=CohortBinding(dimension=dimension, ids=tuple(ids)))


def test_a_product_cohort_is_not_applied_as_account_ids():
    q = compiled("product", ["ZENOVAX", "GEMTARA"])
    assert EXPRESSION["product"] in q.sql
    assert EXPRESSION["account"] not in q.sql, "product names were used as organization ids"
    assert ["ZENOVAX", "GEMTARA"] in q.params


def test_an_account_cohort_is_applied_as_account_ids():
    q = compiled("account", ["ORG-1", "ORG-2"])
    assert EXPRESSION["account"] in q.sql
    assert ["ORG-1", "ORG-2"] in q.params


@pytest.mark.parametrize("dimension", sorted(EXPRESSION))
def test_every_supported_grain_is_applied_through_its_own_expression(dimension):
    q = compiled(dimension, ["A", "B"])
    assert EXPRESSION[dimension] in q.sql
    for other, expression in EXPRESSION.items():
        if other != dimension:
            assert expression not in q.sql, f"{dimension} leaked into {other}'s filter"


def test_every_grain_the_continuity_layer_can_freeze_has_an_expression():
    """A grain the continuity layer would freeze but the compiler could not
    apply would be a frozen cohort that silently restricts nothing."""
    assert set(COHORT_FILTER_FIELD) <= set(EXPRESSION)


def test_a_cohort_of_more_than_two_hundred_is_applied_whole():
    """The reason for the new mechanism: the plan's account filter stops at
    200."""
    ids = [f"GP{i:04d}" for i in range(500)]
    q = compiled("account", ids)
    assert ids in q.params


def test_an_empty_cohort_matches_nothing_rather_than_everything():
    q = compiled("account", [])
    assert "FALSE" in q.sql


def test_a_cohort_is_applied_to_both_sides_of_a_share_where_the_filter_would_be():
    """A product cohort restricts the numerator's products; the market
    denominator widens as it does for a product_names filter."""
    q = compiled("product", ["ZENOVAX"], metric="brand_market_share")
    assert q.sql.count(EXPRESSION["product"]) >= 1


# ---------------------------------------------------------------------------
# The planners no longer copy ids
# ---------------------------------------------------------------------------

def plan_follow_up(dimension, cohort, question="Show me those same ones by month"):
    from app.conversation.continuity import Cohort, resolve
    previous = {"metric": "paid_pack_units", "filters": {}, "dimensions": ["account"],
                "time": R3M, "ranking": {"direction": "top", "limit": 10}}
    cont = resolve(question, previous_plan=previous,
                   cohort=Cohort(dimension=dimension or "", ids=tuple(cohort)))
    context = PlanningContext(
        role="exec", scope_description="all territories and regions",
        wac_authorized=True, reporting_anchor=ANCHOR,
        known_products=["ZENOVAX", "GEMTARA"],
        previous_plan=previous,
        previous_cohort=list(cohort), previous_cohort_dimension=dimension,
        continuity=cont,
    )
    return OfflinePlanner().plan(question, context).plan, cont


@pytest.mark.parametrize("dimension", sorted(COHORT_FILTER_FIELD))
def test_the_offline_planner_leaves_the_population_to_the_server(dimension):
    plan, cont = plan_follow_up(dimension, ["A", "B"])
    assert cont.carries_cohort
    for field_name in COHORT_FILTER_FIELD.values():
        assert getattr(plan.filters, field_name) == [], field_name


def test_a_frozen_cohort_drops_the_ranking():
    plan, _ = plan_follow_up("account", ["ORG-1", "ORG-2"])
    assert plan.ranking is None


def test_a_period_cohort_is_not_a_population():
    """"Those same months" is a time window, not a set of entities."""
    _, cont = plan_follow_up("period_mo", ["2026-08", "2026-09"])
    assert not cont.carries_cohort


def test_an_untyped_cohort_is_not_guessed_at():
    """Older turns have no recorded grain. Applying them anywhere is a guess."""
    _, cont = plan_follow_up(None, ["ORG-1", "ORG-2"])
    assert not cont.carries_cohort


def test_a_fresh_question_does_not_inherit_the_cohort():
    _, cont = plan_follow_up("account", ["ORG-1"],
                             question="What are our total pack units this quarter?")
    assert not cont.carries_cohort


def test_modifying_the_previous_request_reranks_rather_than_freezes():
    """"What about last quarter?" is the same question in a new window. Only
    an explicit reference -- "those", "the same ones" -- freezes."""
    plan, cont = plan_follow_up("account", ["ORG-1", "ORG-2"],
                                question="What about last quarter?")
    assert not cont.carries_cohort
    assert plan.ranking is not None
