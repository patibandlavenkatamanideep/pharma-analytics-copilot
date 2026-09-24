"""Does the plan answer the question that was asked?

A plan can be valid, compile cleanly, execute quickly and return a confident
number that answers a DIFFERENT question. That is the worst failure this system
has, because nothing downstream can detect it: the SQL is correct, the
authorization is correct, the rendering is correct.

Two ways it happened here, both observed:

  * "What is the volume for FLOOBERTAX this quarter?" -- a product that does
    not exist. The planner dropped the filter it could not resolve and the
    answer was 484,394 packs: the whole company, presented as that product's
    volume, with nothing said.

  * "What percentage of our volume comes from 340B accounts?" -- answered with
    59,419 packs. A percentage was asked for and a count was returned.

This module compares the QUESTION with the PLAN and reports the gaps. It does
not guess what the user meant; it says what it could not honour, so the caller
can ask rather than answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                  # pragma: no cover
    from app.analytics.entities import Vocabulary
    from app.analytics.plan import AnalyticalPlan

# ALL-CAPS tokens that are vocabulary of the domain rather than entity names.
# A token here is never treated as an unrecognised product.
KNOWN_ACRONYMS = frozenset({
    "GPO", "WAC", "PAP", "NDC", "YTD", "MTD", "QTD", "340B", "IDN", "US", "USA",
    "R3M", "R6M", "Q1", "Q2", "Q3", "Q4", "FY", "HCP", "HCO", "ID", "SKU",
    "TRX", "NRX", "COGS", "ASP", "AMP", "IV", "PO", "MG", "ML", "OK", "FAQ",
})

# The metrics that are genuinely proportions.
RATIO_METRICS = frozenset({"brand_market_share", "pap_proportion", "share_trend_pp"})

_PROPORTION_ASKED = re.compile(
    r"\bwhat (?:percent|percentage)\b|\bwhat (?:share|proportion|fraction)\b"
    r"|\bpercentage of\b|\bproportion of\b|\bfraction of\b|\bshare of\b"
    r"|\bwhat % \b|\bhow much of\b",
    re.I,
)
_THRESHOLD_ASKED = re.compile(
    r"\b(?:more|greater|larger|higher|less|fewer|smaller|lower) than\b"
    r"|\bat least\b|\bat most\b|\bover \d|\bunder \d|\babove \d|\bbelow \d",
    re.I,
)
_TERRITORY_SLOT = re.compile(
    r"\b(?:in|for|from|across)\s+(?:the\s+)?([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,3})"
    r"\s+(territory|territories|region|regions)\b"
)
_ALLCAPS = re.compile(r"\b([A-Z][A-Z0-9'&-]{2,})\b")

# Words that name a product classification. If the question uses one and the
# plan carries no matching classification filter, the qualifier was dropped --
# and a qualifier is usually the whole point of the question. "What is the
# GENERIC share in Platinum Compounds" answered as company brand share is the
# same number the question was asking to exclude.
CLASSIFICATION_WORDS = {
    "generic": "generic",
    "generics": "generic",
    "biosimilar": "biosimilar",
    "biosimilars": "biosimilar",
    "branded competitor": "branded_competitor",
    "branded competitors": "branded_competitor",
    "competitor": "branded_competitor",
    "competitors": "branded_competitor",
    "our brand": "company_brand",
    "company brand": "company_brand",
}


@dataclass(frozen=True)
class IntentGap:
    kind: str
    detail: str
    suggestion: str = ""

    def message(self) -> str:
        return f"{self.detail} {self.suggestion}".strip()


def _normalise(values) -> set[str]:
    return {str(v).strip().upper() for v in values if str(v).strip()}


def _plan_filter_values(plan: "AnalyticalPlan") -> set[str]:
    f = plan.filters
    out: set[str] = set()
    for field_name in ("product_names", "ndcs", "market_categories",
                       "market_subcategories", "gpo_names", "org_archetypes",
                       "territories", "regions", "states", "classifications"):
        out |= _normalise(getattr(f, field_name, []) or [])
    return out


def _closest(term: str, options, limit: int = 3) -> list[str]:
    """Near matches, by whole string and by word.

    Whole-string similarity alone misses the common case: a multi-word place
    name where the user typed one of the words. "Atlantia" is not similar to
    "Mid-Atlantic West" overall, but it is similar to "Mid-Atlantic". A
    suggestion is the difference between a dead end and a second try.
    """
    import difflib

    options = [str(o) for o in options]
    hits = difflib.get_close_matches(term, options, n=limit, cutoff=0.6)
    if len(hits) >= limit:
        return hits

    lowered = term.lower()
    for option in options:
        if option in hits:
            continue
        words = re.split(r"[\s/&-]+", option.lower())
        if any(difflib.SequenceMatcher(None, lowered, w).ratio() >= 0.6 for w in words):
            hits.append(option)
        if len(hits) >= limit:
            break
    return hits


def find_gaps(
    question: str,
    plan: "AnalyticalPlan",
    vocabulary: "Vocabulary",
) -> list[IntentGap]:
    """Everything the question asked for that the plan does not deliver."""
    gaps: list[IntentGap] = []
    applied = _plan_filter_values(plan)

    known_products = _normalise(vocabulary.products)
    known_places = _normalise(vocabulary.all_territories) | _normalise(vocabulary.all_regions)
    known_other = (
        _normalise(vocabulary.subcategories) | _normalise(vocabulary.categories)
        | _normalise(vocabulary.gpos) | _normalise(vocabulary.archetypes)
    )
    everything_known = known_products | known_places | known_other

    # --- a named place that is not a place ---------------------------------
    for match in _TERRITORY_SLOT.finditer(question):
        name, kind = match.group(1).strip(), match.group(2).lower()
        if name.upper() in everything_known or name.upper() in applied:
            continue
        is_region = kind.startswith("region")
        pool = vocabulary.all_regions if is_region else vocabulary.all_territories
        noun = "region" if is_region else "territory"
        near = _closest(name, pool)
        gaps.append(IntentGap(
            kind="unresolved_place",
            detail=f'No {noun} called "{name}" exists in this dataset.',
            suggestion=f"Did you mean {', '.join(near)}?" if near else "",
        ))

    # --- a named product that is not a product -----------------------------
    for token in _ALLCAPS.findall(question):
        upper = token.upper()
        if upper in KNOWN_ACRONYMS or upper in everything_known or upper in applied:
            continue
        if upper.isdigit():
            continue
        near = _closest(token, vocabulary.products)
        gaps.append(IntentGap(
            kind="unresolved_product",
            detail=f'"{token}" is not a product in this dataset.',
            suggestion=f"Did you mean {', '.join(near)}?" if near else "",
        ))

    # --- a proportion asked for, a count returned --------------------------
    if _PROPORTION_ASKED.search(question) and plan.metric.value not in RATIO_METRICS:
        gaps.append(IntentGap(
            kind="unsupported_proportion",
            detail=(
                "This asks for a proportion, and the available metrics cannot "
                "express one for this breakdown."
            ),
            suggestion=(
                "The figure below is a count, not a percentage. Asking for the "
                "breakdown (for example \"volume by 340B status\") gives both "
                "components, which can be divided."
            ),
        ))

    # --- a named classification the plan did not honour --------------------
    asked_classes = {
        value for word, value in CLASSIFICATION_WORDS.items()
        if re.search(rf"\b{re.escape(word)}\b", question, re.I)
    }
    planned_classes = _normalise(plan.filters.classifications or [])
    unhonoured = {c for c in asked_classes if c.upper() not in planned_classes}
    if unhonoured:
        named = ", ".join(sorted(c.replace("_", " ") for c in unhonoured))
        gaps.append(IntentGap(
            kind="unhonoured_classification",
            detail=(
                f"This asks about {named} products, but the figure is not "
                f"restricted to them."
            ),
            suggestion=(
                "brand_flag = 0 covers branded competitors as well as generics, "
                "so the distinction comes from the derived product "
                "classification rather than from the source data."
            ),
        ))

    # --- a threshold asked for, no threshold expressible -------------------
    if _THRESHOLD_ASKED.search(question):
        gaps.append(IntentGap(
            kind="unsupported_threshold",
            detail="This asks for a threshold, which the plan cannot express.",
            suggestion=(
                "The result is not filtered by that condition. Ranking "
                "(\"top 10 by volume\") is supported and is usually close."
            ),
        ))

    return gaps


def blocking(gaps: list[IntentGap]) -> list[IntentGap]:
    """Gaps that make an answer misleading rather than merely incomplete.

    A named entity that does not exist is blocking: dropping the filter answers
    a broader question, and the number returned looks like an answer to the
    narrow one. A missing proportion or threshold is disclosed instead, because
    the number returned is still true -- it is just not the whole question.
    """
    return [g for g in gaps if g.kind in ("unresolved_place", "unresolved_product")]
