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

#: Units that answer "what percentage". Read from the metric REGISTRY, not
#: from a list of metric names kept here. The list this replaced named three
#: metrics and went stale: share_340b, market_segment_share and
#: volume_growth are all proportions, and a correct answer from any of them
#: carried a note saying "The figure below is a count, not a percentage".
PROPORTION_UNITS = frozenset({"ratio", "percentage points"})


def _is_proportion(metric_key: str) -> bool:
    from app.analytics.registry import get_registry
    return get_registry().get(metric_key).get("unit") in PROPORTION_UNITS

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
        # Deliberately NOT skipped because the plan carries it: a filter value
        # the planner invented is not evidence that the place exists.
        if name.upper() in everything_known:
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
        # Checked against the VOCABULARY, never against the plan. A model that
        # confidently puts FLOOBERTAX in product_names has not made FLOOBERTAX
        # real -- and skipping tokens the plan already carried meant exactly
        # that hallucination passed, and the answer came back "unavailable"
        # instead of asking what was meant.
        if upper in KNOWN_ACRONYMS or upper in everything_known:
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
    if _PROPORTION_ASKED.search(question) and not _is_proportion(plan.metric.value):
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

    # --- a named therapeutic area the plan did not honour ------------------
    named_specialties = {
        v for v in (vocabulary.specialties or [])
        if re.search(rf"\b{re.escape(v)}\b", question, re.I)
    }
    planned_specialties = _normalise(plan.filters.specialties or [])
    dropped = {v for v in named_specialties if v.upper() not in planned_specialties}
    if dropped:
        gaps.append(IntentGap(
            kind="unhonoured_specialty",
            detail=(
                f"This asks about {', '.join(sorted(dropped))} products, but the "
                f"figure is not restricted to them."
            ),
            suggestion="",
        ))

    # --- a threshold: asked for, and did it survive intact? ----------------
    gaps += _threshold_gaps(question, plan)

    # --- a rolling average, which the plan cannot express ------------------
    if plan.rolling is None and re.search(
            r"\brolling\b|\bmoving average\b|\btrailing average\b|"
            r"\b\d+[- ](?:month|week|quarter) average\b", question, re.I):
        gaps.append(IntentGap(
            kind="unsupported_rolling_average",
            detail=(
                "A rolling average is not something the plan can express, so the "
                "figure below is a total for the window, not an average of it."
            ),
            suggestion=(
                "Asking for the series (\"volume by month\") gives the points a "
                "rolling average would be computed from."
            ),
        ))

    return gaps


def _threshold_gaps(question: str, plan: "AnalyticalPlan") -> list[IntentGap]:
    """Did the bound the question states reach the plan unchanged?

    Three ways it can fail, with different consequences:

    * **direction** -- "more than" planned as "less than". The answer is about
      the complementary population. Blocking.
    * **value** -- "more than 20%" planned against a share as 20 rather than
      0.2 returns nothing, and planned as 0.02 returns nearly everything.
      Blocking.
    * **boundary** -- "at least 100" planned as "more than 100". Only the rows
      exactly AT the bound differ, and those are the ones a reader checks.
      Disclosed, not blocked: the rest of the answer is right, and the note
      says precisely which rows are missing.

    Read with app.analytics.thresholds, the table the planners use, so the
    guard and the planner cannot disagree about what a phrase means.
    """
    from app.analytics.registry import get_registry
    from app.analytics.thresholds import (
        PHRASE_FOR, expected_value, read_threshold, same_direction)

    read = read_threshold(question)
    if read is None:
        if _THRESHOLD_ASKED.search(question) and plan.threshold is None:
            return [IntentGap(
                kind="unsupported_threshold",
                detail="This asks for a threshold that could not be read from the question.",
                suggestion=(
                    "The result is not filtered by that condition. Stating it as a "
                    "number (\"more than 500 packs\", \"declined more than 20%\") "
                    "is understood."
                ),
            )]
        return []

    if read.needs_range:
        return [IntentGap(
            kind="unsupported_threshold",
            detail=(
                f'"{read.phrase}" alongside a decline describes a range -- a fall, '
                f"but a small one -- which one threshold cannot express."
            ),
            suggestion=(
                "The result is not filtered by it. Asking for the decliners and "
                "the size of each decline shows the same thing."
            ),
        )]

    if plan.threshold is None:
        return [IntentGap(
            kind="unsupported_threshold",
            detail=f'The result is not filtered to "{read.phrase}".',
            suggestion=(
                "A threshold needs a breakdown to filter -- by account, product "
                "or territory, for example."
            ),
        )]

    gaps: list[IntentGap] = []
    unit = get_registry().get(plan.metric.value).get("unit", "")
    if not same_direction(read.op, plan.threshold.op):
        gaps.append(IntentGap(
            kind="threshold_direction_mismatch",
            detail=(
                f'The question asks for "{read.phrase}", but the plan filters '
                f"for values {PHRASE_FOR[plan.threshold.op]} "
                f"{plan.threshold.value:g} -- the opposite side of the bound."
            ),
        ))
    want = expected_value(read, unit)
    if want is None or abs(plan.threshold.value - want) > 1e-9 * max(1.0, abs(want)):
        gaps.append(IntentGap(
            kind="threshold_value_mismatch",
            detail=(
                f'The question sets the bound at "{read.phrase}"'
                + (f", which is {want:g} in {unit}" if want is not None
                   else f", which cannot be expressed in {unit}")
                + f"; the plan used {plan.threshold.value:g}."
            ),
        ))
    if not gaps and read.op != plan.threshold.op:
        included = plan.threshold.op.endswith("e")
        gaps.append(IntentGap(
            kind="threshold_boundary",
            detail=(
                f'Read "{read.phrase}" as {PHRASE_FOR[plan.threshold.op]} '
                f"{plan.threshold.value:g}: a value of exactly "
                f"{plan.threshold.value:g} is "
                + ("included." if included else "not included.")
            ),
        ))
    return gaps


def blocking(gaps: list[IntentGap]) -> list[IntentGap]:
    """Gaps that make an answer misleading rather than merely incomplete.

    A named entity that does not exist is blocking: dropping the filter answers
    a broader question, and the number returned looks like an answer to the
    narrow one. A dropped therapeutic area is blocking for the same reason --
    "our oncology portfolio" answered with every product is a wrong number, not
    an incomplete one. A missing proportion or threshold is disclosed instead,
    because the number returned is still true; it is just not the whole
    question.
    """
    return [g for g in gaps
            if g.kind in ("unresolved_place", "unresolved_product",
                          "unhonoured_specialty",
                          "threshold_direction_mismatch",
                          "threshold_value_mismatch")]
