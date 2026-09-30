"""Finding what a question names, in any casing, before planning.

Review finding 2: "FLOOBERTAX" was blocked, "Floobertax" and "floobertax"
were answered with the whole company's volume under the product's name. The
guard recognised an unknown product only in capitals and knew nothing about
accounts. These pin the mention finder against a hand-built index, so every
case is visible here rather than depending on what the dataset contains.
"""

from __future__ import annotations

import pytest

from app.analytics.mentions import AccountRef, find_mentions, index_from, normalise

INDEX = index_from(
    products=["ZENOVAX", "PAXELIUM", "DOCETAXEL GENERIC"],
    accounts=[
        AccountRef("GP001", "Memorial Health System", "6 facilities, NY"),
        AccountRef("GP009", "St Marys Health", "3 facilities, OH"),
        # Two different organisations, one name.
        AccountRef("SA0101", "Riverside Clinic", "1 facility, TX"),
        AccountRef("SA0202", "Riverside Clinic", "1 facility, OR"),
    ],
    other_vocabulary=["Docetaxel", "Oncology", "Great Lakes", "Midwest", "NovaPharma"],
)
NO_ACCOUNTS = index_from(["ZENOVAX"], None, ["Oncology"])


def kinds(question, index=INDEX):
    return [(m.kind, m.text) for m in find_mentions(question, index)]


# ---------------------------------------------------------------------------
# Unknown products, in every casing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["FLOOBERTAX", "Floobertax", "floobertax"])
@pytest.mark.parametrize("template", [
    "What is the volume for {} this quarter?",
    "Show me {} sales in Q3",
    "How is {} doing this year?",
])
def test_an_unknown_product_is_found_in_any_casing(spelling, template):
    assert ("unknown_product", spelling) in kinds(template.format(spelling))


@pytest.mark.parametrize("question", [
    "What is the volume for each territory this quarter?",      # stopword
    "What was our volume for Oncology this quarter?",           # specialty
    "What is the volume for Texas this quarter?",               # state
    "Rank all territories by total NovaPharma volume",          # the company
    "Show me the Docetaxel market share",                       # subcategory
    "What were total sales last month?",
])
def test_ordinary_words_in_a_product_slot_are_not_products(question):
    assert not [k for k, _ in kinds(question) if k.startswith("unknown")]


# ---------------------------------------------------------------------------
# Known products
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["ZENOVAX", "Zenovax", "zenovax", "Zenovax's"])
def test_a_known_product_is_found_in_any_casing_or_possessive(text):
    found = find_mentions(f"What is {text} volume this quarter?", INDEX)
    assert [(m.kind, m.ids) for m in found] == [("product", ("ZENOVAX",))]


def test_the_longest_name_wins():
    """'DOCETAXEL GENERIC' is a product; 'Docetaxel' alone is a market."""
    found = find_mentions("How is docetaxel generic volume trending?", INDEX)
    assert [(m.kind, m.ids) for m in found] == [("product", ("DOCETAXEL GENERIC",))]


def test_a_product_named_as_a_comparison_point_is_a_reference():
    found = find_mentions("Which products gained share versus ZENOVAX?", INDEX)
    assert found[0].kind == "product" and found[0].reference_only


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Memorial Health System", "memorial health system",
                                  "MEMORIAL  HEALTH   SYSTEM"])
def test_an_account_is_found_by_full_name_in_any_casing(text):
    found = find_mentions(f"What was the volume for {text} last quarter?", INDEX)
    assert [(m.kind, m.ids) for m in found] == [("account", ("GP001",))]


def test_punctuation_and_possessives_do_not_block_a_match():
    found = find_mentions("Volume for St. Mary's Health this year", INDEX)
    assert [(m.kind, m.ids) for m in found] == [("account", ("GP009",))]


def test_a_shared_name_is_ambiguous_with_every_candidate():
    """Identical names are not identical organisations."""
    (m,) = find_mentions("What was the volume for Riverside Clinic last month?", INDEX)
    assert m.ambiguous
    assert set(m.ids) == {"SA0101", "SA0202"}
    assert {c.detail for c in m.candidates} == {"1 facility, TX", "1 facility, OR"}


def test_a_partial_name_is_not_a_match():
    """Fuzzy-matching a restricted catalog would turn a typo into someone
    else's account. 'Memorial' is not 'Memorial Health System'."""
    found = find_mentions("What was the volume for Memorial this quarter?", INDEX)
    assert not [m for m in found if m.kind == "account"]


def test_an_unmatched_account_phrase_is_reported():
    found = kinds("What was the volume for Northwind Regional Health last month?")
    assert ("unknown_account", "Northwind Regional Health") in found


def test_without_an_account_index_an_account_phrase_says_nothing():
    """Nothing was loaded to match against, so an unmatched phrase is not
    evidence of anything."""
    found = kinds("What was the volume for Northwind Regional Health last month?",
                  NO_ACCOUNTS)
    assert not [k for k, _ in found if k == "unknown_account"]


@pytest.mark.parametrize("question", [
    "What was the volume for New York last quarter?",
    "What was the volume for Great Lakes last quarter?",
    "Show volume for Q3 2026",
])
def test_a_place_or_period_in_an_account_slot_is_not_an_account(question):
    assert not [k for k, _ in kinds(question) if k == "unknown_account"]


def test_normalise_is_case_space_and_punctuation_insensitive():
    assert normalise("  St. Mary’s  Health & Care ") == normalise("st marys health and care")
