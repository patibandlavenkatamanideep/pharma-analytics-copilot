"""Every entity the question names must survive into the plan.

Review finding 2: "A named account can also disappear from the filters. A
known product deliberately omitted from a plan is not caught by the guard."
A plan that drops a named entity answers a broader question under the narrow
question's heading -- the company's volume, labelled as one account's.
"""

from __future__ import annotations

import pytest

from app.analytics import intent
from app.analytics.entities import Vocabulary
from app.analytics.mentions import AccountRef, index_from
from app.analytics.plan import AnalyticalPlan

VOCAB = Vocabulary(products=["ZENOVAX", "PAXELIUM"], specialties=["Oncology"])
INDEX = index_from(
    VOCAB.products,
    [AccountRef("GP001", "Memorial Health System", "6 facilities, NY"),
     AccountRef("SA0101", "Riverside Clinic", "1 facility, TX"),
     AccountRef("SA0202", "Riverside Clinic", "1 facility, OR")],
    ["Oncology"],
)
R3M = {"kind": "named", "named": "r3m"}


def plan(**kw):
    return AnalyticalPlan.model_validate({"metric": "paid_pack_units", "time": R3M, **kw})


def blocking_kinds(question, p):
    return {g.kind for g in intent.blocking(intent.find_gaps(question, p, VOCAB, INDEX))}


# ---------------------------------------------------------------------------
# Dropped
# ---------------------------------------------------------------------------

def test_a_named_product_missing_from_the_plan_blocks():
    assert "dropped_product" in blocking_kinds("What is Zenovax volume this quarter?", plan())


def test_a_named_product_in_the_plan_passes():
    assert not blocking_kinds("What is Zenovax volume this quarter?",
                              plan(filters={"product_names": ["ZENOVAX"]}))


def test_a_product_breakdown_includes_the_named_product_without_a_filter():
    """'How does ZENOVAX compare with our other products' -- a breakdown by
    product shows ZENOVAX as a row, so it has not been dropped."""
    assert "dropped_product" not in blocking_kinds(
        "How does Zenovax compare with our other products?",
        plan(dimensions=["product"]))


def test_a_reference_product_is_not_required_as_a_filter():
    assert "dropped_product" not in blocking_kinds(
        "Which accounts buy more than they did versus Zenovax?", plan())


def test_a_named_account_missing_from_the_plan_blocks():
    assert "dropped_account" in blocking_kinds(
        "What was the volume for Memorial Health System last quarter?", plan())


def test_a_named_account_in_the_plan_passes():
    assert not blocking_kinds(
        "What was the volume for Memorial Health System last quarter?",
        plan(filters={"account_ids": ["GP001"]}))


# ---------------------------------------------------------------------------
# Invented
# ---------------------------------------------------------------------------

def test_a_product_the_plan_invented_blocks():
    assert "invented_product" in blocking_kinds(
        "What is the volume this quarter?", plan(filters={"product_names": ["FLOOBERTAX"]}))


def test_an_account_outside_the_index_blocks():
    """The model does not know the account ids; one it produced came from
    nowhere the user can see."""
    assert "invented_account" in blocking_kinds(
        "What is the volume this quarter?", plan(filters={"account_ids": ["GP999"]}))


# ---------------------------------------------------------------------------
# Ambiguous
# ---------------------------------------------------------------------------

def test_a_shared_name_blocks_with_the_real_choices():
    gaps = intent.find_gaps("What was the volume for Riverside Clinic last month?",
                            plan(), VOCAB, INDEX)
    (gap,) = [g for g in gaps if g.kind == "ambiguous_account"]
    assert {c[0] for c in gap.choices} == {"SA0101", "SA0202"}
    assert gap in intent.blocking(gaps)


def test_a_plan_that_picked_one_of_the_candidates_resolves_it():
    assert "ambiguous_account" not in blocking_kinds(
        "What was the volume for Riverside Clinic last month?",
        plan(filters={"account_ids": ["SA0202"]}))


# ---------------------------------------------------------------------------
# Before planning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,kind", [
    ("What is the volume for floobertax this quarter?", "unresolved_product"),
    ("What was the volume for Northwind Regional Health last month?", "unresolved_account"),
    ("What was the volume for Riverside Clinic last month?", "ambiguous_account"),
])
def test_what_no_plan_could_fix_is_found_before_planning(question, kind):
    _, gaps = intent.resolve_mentions(question, VOCAB, INDEX)
    assert kind in {g.kind for g in gaps}


def test_a_resolvable_question_has_nothing_to_ask_before_planning():
    mentions, gaps = intent.resolve_mentions(
        "What was the volume for Memorial Health System last quarter?", VOCAB, INDEX)
    assert gaps == []
    assert [(m.kind, m.ids) for m in mentions] == [("account", ("GP001",))]


def test_only_the_named_accounts_reach_the_live_prompt():
    """The prompt carries the ids the question names, never the catalog."""
    from app.llm.planner import PlanningContext, build_system_prompt
    text = build_system_prompt(PlanningContext(
        role="exec", scope_description="all", wac_authorized=True,
        reporting_anchor={"min_mo": 0, "max_mo": 36, "min_wk": 0, "max_wk": 155,
                          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"},
        named_accounts=[("Memorial Health System", "GP001")]))
    assert "GP001" in text
    assert "SA0101" not in text and "Riverside" not in text
