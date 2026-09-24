"""The plan must answer the question that was asked.

The failure these guard against is the one nothing downstream can detect: a
plan that is valid, compiles cleanly, is correctly scoped, and returns a
confident number for a different question.

Observed before this check existed:
  "What is the volume for FLOOBERTAX this quarter?"  -> 484,394 packs
  (the whole company, presented as that product, with nothing said)
  "What percentage of our volume comes from 340B accounts?" -> 59,419 packs
  "What is the generic share in Platinum Compounds?" -> company brand share
"""

from __future__ import annotations

import pytest

from app.analytics.entities import Vocabulary
from app.analytics.intent import blocking, find_gaps
from app.analytics.plan import AnalyticalPlan

VOCAB = Vocabulary(
    products=["ZENOVAX", "GEMTARA", "NOVATAXEL"],
    subcategories=["Docetaxel", "Paclitaxel"],
    categories=["Platinum Compounds"],
    gpos=["Vizient"],
    archetypes=["Academic"],
    territories=["Mid-Atlantic West"],
    regions=["East"],
    all_territories=["Mid-Atlantic West", "Pacific North"],
    all_regions=["East", "West"],
)


def plan_for(metric="paid_pack_units", **filters):
    return AnalyticalPlan.model_validate({
        "metric": metric,
        "filters": filters,
        "time": {"kind": "named", "named": "r3m"},
    })


def kinds(question, plan):
    return {g.kind for g in find_gaps(question, plan, VOCAB)}


# ---------------------------------------------------------------------------
# An entity that does not exist must not silently widen the question
# ---------------------------------------------------------------------------

def test_an_unknown_product_is_a_blocking_gap():
    gaps = find_gaps("What is the volume for FLOOBERTAX this quarter?",
                     plan_for(), VOCAB)
    assert {g.kind for g in gaps} == {"unresolved_product"}
    assert blocking(gaps), "answering would report the whole company as that product"


def test_a_known_product_that_was_resolved_is_not_flagged():
    assert kinds("What is the volume for ZENOVAX this quarter?",
                 plan_for(product_names=["ZENOVAX"])) == set()


def test_a_known_product_the_planner_dropped_is_not_flagged_as_unknown():
    """It is in the vocabulary, so this check has nothing to say about it."""
    assert "unresolved_product" not in kinds(
        "What is the volume for ZENOVAX this quarter?", plan_for())


def test_an_unknown_territory_is_blocking_and_suggests_a_near_match():
    gaps = find_gaps("Show me volume in the Atlantia territory", plan_for(), VOCAB)
    assert [g.kind for g in gaps] == ["unresolved_place"]
    assert "Mid-Atlantic West" in gaps[0].suggestion


def test_a_real_territory_is_not_flagged():
    assert kinds("Show me volume in the Mid-Atlantic West territory",
                 plan_for(territories=["Mid-Atlantic West"])) == set()


@pytest.mark.parametrize("acronym", ["GPO", "WAC", "PAP", "NDC", "YTD", "IDN"])
def test_domain_acronyms_are_not_mistaken_for_products(acronym):
    assert kinds(f"Show me volume by {acronym} this quarter", plan_for()) == set()


def test_340b_is_not_mistaken_for_a_product():
    assert "unresolved_product" not in kinds(
        "Show me volume excluding 340B accounts", plan_for())


# ---------------------------------------------------------------------------
# A proportion asked for, a count returned
# ---------------------------------------------------------------------------

def test_a_percentage_question_answered_with_a_count_is_disclosed():
    gaps = find_gaps(
        "What percentage of our volume comes from 340B accounts this quarter?",
        plan_for(), VOCAB)
    assert {g.kind for g in gaps} == {"unsupported_proportion"}
    # Disclosed, not blocked: the count is true, it is just not the question.
    assert not blocking(gaps)


def test_a_percentage_question_answered_with_a_ratio_is_fine():
    assert kinds("What percentage of the market is ours?",
                 plan_for("brand_market_share")) == set()


# ---------------------------------------------------------------------------
# A qualifier the plan did not honour
# ---------------------------------------------------------------------------

def test_generic_share_answered_as_brand_share_is_disclosed():
    """amb-01. brand_flag = 0 covers branded competitors too, so 'generic'
    needs the derived classification -- dropping it answers the opposite."""
    gaps = find_gaps("What is the generic share in the Platinum Compounds market?",
                     plan_for("brand_market_share",
                              market_categories=["Platinum Compounds"]), VOCAB)
    assert "unhonoured_classification" in {g.kind for g in gaps}


def test_generic_share_with_the_filter_applied_is_not_flagged():
    assert "unhonoured_classification" not in kinds(
        "What is the generic share in the Platinum Compounds market?",
        plan_for("brand_market_share",
                 market_categories=["Platinum Compounds"],
                 classifications=["generic"]))


def test_a_biosimilar_question_is_checked_too():
    assert "unhonoured_classification" in kinds(
        "How are biosimilars performing?", plan_for())


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "Show me accounts with more than 500 packs this quarter",
    "Which accounts bought at least 100 units?",
    "Accounts with fewer than 10 packs",
])
def test_a_threshold_is_disclosed_as_unexpressible(question):
    gaps = find_gaps(question, plan_for(), VOCAB)
    assert "unsupported_threshold" in {g.kind for g in gaps}
    assert not blocking(gaps)


def test_a_plain_ranking_is_not_mistaken_for_a_threshold():
    assert kinds("What are the top 10 accounts by pack units this quarter?",
                 plan_for()) == set()


# ---------------------------------------------------------------------------
# Ordinary questions produce no noise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "What are our top 5 accounts by pack units this quarter?",
    "Show me the monthly volume trend for the last six months",
    "Compare Q1 2026 and Q2 2026 pack units",
    "How did we do?",
])
def test_ordinary_questions_raise_no_gaps(question):
    assert kinds(question, plan_for()) == set(), question
