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
    from app.analytics.mentions import EntityIndex
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
    #: For an ambiguous name: the candidates, as (entity_id, label, detail),
    #: so the user chooses between real options rather than retyping.
    choices: tuple[tuple[str, str, str], ...] = ()
    #: The phrase the gap is about, as the user typed it -- stored with a
    #: pending clarification so a reply can be matched back to it.
    subject: str = ""

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
    index: "EntityIndex | None" = None,
    resolved: dict[str, str] | None = None,
) -> list[IntentGap]:
    """Everything the question asked for that the plan does not deliver.

    `index` is the caller's entity index (app.analytics.mentions), built
    under their scope. Without one, products are still checked -- they come
    from the vocabulary -- but accounts cannot be.
    """
    from app.analytics.mentions import index_from

    gaps: list[IntentGap] = []
    if index is None:
        index = index_from(vocabulary.products, None, _other_vocabulary(vocabulary))
    gaps += fidelity_gaps(question, plan, index, vocabulary, resolved)

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
    # ALL-CAPS anywhere reads as a product name in this domain. The slot rule
    # in mentions.py catches the other casings; this keeps catching a capital
    # name outside a slot. One gap per token either way.
    already = {g.detail for g in gaps if g.kind == "unresolved_product"}
    for token in _ALLCAPS.findall(question):
        upper = token.upper()
        if any(f'"{token}"' in d or f'"{upper}"' in d for d in already):
            continue
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

    # --- the shape: broken down as asked, ranked as asked? -----------------
    gaps += _shape_gaps(question, plan)

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


def _other_vocabulary(vocabulary: "Vocabulary") -> list[str]:
    return (list(vocabulary.subcategories) + list(vocabulary.categories)
            + list(vocabulary.specialties) + list(vocabulary.gpos)
            + list(vocabulary.archetypes) + list(vocabulary.all_territories)
            + list(vocabulary.all_regions) + list(vocabulary.company_names))


def fidelity_gaps(
    question: str,
    plan: "AnalyticalPlan",
    index: "EntityIndex",
    vocabulary: "Vocabulary",
    resolved: dict[str, str] | None = None,
) -> list[IntentGap]:
    """Did every named product and account survive into the plan -- and does
    everything the plan filters on exist?

    Checked against the INDEX, never against the plan's own values: a model
    that puts FLOOBERTAX in product_names has not made it real.
    """
    from app.analytics.mentions import apply_choices, find_mentions, normalise
    from app.analytics.plan import Dimension

    gaps: list[IntentGap] = []
    planned_products = {normalise(p) for p in plan.filters.product_names}
    planned_accounts = set(plan.filters.account_ids)
    by_product = Dimension.product in plan.dimensions
    by_account = Dimension.account in plan.dimensions

    for m in apply_choices(find_mentions(question, index), resolved):
        if m.kind == "unknown_product":
            near = _closest(m.text.upper(), vocabulary.products)
            gaps.append(IntentGap(
                kind="unresolved_product",
                detail=f'"{m.text}" is not a product in this dataset.',
                suggestion=f"Did you mean {', '.join(near)}?" if near else "",
            ))
        elif m.kind == "unknown_account":
            gaps.append(IntentGap(
                kind="unresolved_account",
                detail=f'No account called "{m.text}" is available to you.',
                suggestion="Accounts are matched by their full name.",
            ))
        elif m.kind == "account" and m.ambiguous:
            chosen = planned_accounts & set(m.ids)
            if len(chosen) != 1:
                gaps.append(IntentGap(
                    kind="ambiguous_account",
                    detail=(f'"{m.text}" is the name of {len(m.ids)} different '
                            f"accounts."),
                    suggestion="Which one did you mean?",
                    choices=tuple((c.entity_id, c.label, c.detail) for c in m.candidates),
                    subject=m.text,
                ))
        elif m.reference_only:
            continue
        elif m.kind == "product":
            if normalise(m.ids[0]) not in planned_products and not by_product:
                gaps.append(IntentGap(
                    kind="dropped_product",
                    detail=(f"The question names {m.ids[0]}, but the figure is "
                            f"not restricted to it."),
                ))
        elif m.kind == "account":
            if m.ids[0] not in planned_accounts and not by_account:
                gaps.append(IntentGap(
                    kind="dropped_account",
                    detail=(f"The question names {m.text}, but the figure is "
                            f"not restricted to that account."),
                ))

    for name in plan.filters.product_names:
        if normalise(name) not in index.products:
            gaps.append(IntentGap(
                kind="invented_product",
                detail=f'The plan filters on "{name}", which is not a product in this dataset.',
            ))
    if index.has_accounts:
        for entity_id in plan.filters.account_ids:
            if entity_id not in index.account_ids:
                gaps.append(IntentGap(
                    kind="invented_account",
                    detail="The plan filters on an account that is not available to you.",
                ))
    return gaps


#: Found from the question alone. Checked BEFORE planning: there is no plan
#: that could make them go away, so a model call to produce one is wasted.
PRE_PLAN_KINDS = frozenset({"unresolved_product", "unresolved_account",
                            "ambiguous_account"})


def resolve_mentions(question: str, vocabulary: "Vocabulary", index: "EntityIndex",
                     resolved: dict[str, str] | None = None):
    """The named entities, and the gaps that no plan could close.

    Returns (mentions, gaps). Called on the request path before planning, so
    an unknown or ambiguous name is asked about without a model call, and the
    ids of the accounts the question names can be handed to the planner --
    those ids and no others.
    """
    from app.analytics.mentions import find_mentions
    from app.analytics.plan import AnalyticalPlan

    from app.analytics.mentions import apply_choices

    mentions = apply_choices(find_mentions(question, index), resolved)
    # A plan-free check: an empty plan filters nothing, so only the
    # question-side kinds can fire, and they are the ones kept.
    empty = AnalyticalPlan.model_validate(
        {"metric": "paid_pack_units", "time": {"kind": "named", "named": "all_time"}})
    gaps = [g for g in fidelity_gaps(question, empty, index, vocabulary, resolved)
            if g.kind in PRE_PLAN_KINDS]
    return mentions, gaps


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


_PERIODS = {"period_mo", "period_qtr", "period_wk"}


def _shape_gaps(question: str, plan: "AnalyticalPlan") -> list[IntentGap]:
    """A breakdown or a ranking the question asks for and the plan lacks.

    A missing breakdown, or a different "top N", is disclosed: the figure is
    true, it is just not the whole question, and the disclosure is shown
    first. A ranking in the opposite direction blocks -- "the lowest five"
    answered with the highest five is the opposite answer, not part of it.
    """
    from app.analytics.structure import asked_shape

    shape = asked_shape(question)
    planned = set(plan.dimensions)
    gaps: list[IntentGap] = []
    for word, grains in shape.groupings:
        if not planned & set(grains):
            gaps.append(IntentGap(
                kind="grouping_dropped",
                detail=(f"This asks for a breakdown by {word}, but the figure below "
                        f"is not broken down that way."),
            ))
    r = shape.ranking
    if r is None:
        return gaps
    if not planned & set(r.grains):
        gaps.append(IntentGap(
            kind="ranking_dropped",
            detail=(f"This asks for a ranking of {r.word}, but the figure below is "
                    f"not broken down by {r.word}."),
        ))
        return gaps
    asked = "lowest" if r.direction == "bottom" else "highest"
    if plan.ranking is not None and plan.ranking.direction != r.direction:
        given = "lowest" if plan.ranking.direction == "bottom" else "highest"
        gaps.append(IntentGap(
            kind="ranking_direction_mismatch",
            detail=f"This asks for the {asked} {r.word}, but the plan ranks the {given}.",
            suggestion="Ask again, saying which end of the ranking you want.",
        ))
        return gaps
    in_time_order = plan.dimensions and plan.dimensions[0].value in _PERIODS
    if plan.ranking is None and (r.direction == "bottom" or in_time_order):
        order = "in time order" if in_time_order else "largest first"
        gaps.append(IntentGap(
            kind="ranking_dropped",
            detail=f"This asks for the {asked} {r.word}; the answer lists them {order}.",
        ))
    elif r.limit and (plan.ranking is None or plan.ranking.limit != r.limit):
        shown = (f"the {plan.ranking.direction} {plan.ranking.limit}" if plan.ranking
                 else "all of them, largest first")
        gaps.append(IntentGap(
            kind="ranking_limit_changed",
            detail=f"This asks for the {r.direction} {r.limit} {r.word}; the answer shows {shown}.",
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
            if g.kind in BLOCKING_KINDS]


#: Gaps after which any figure answers a different question than was asked.
BLOCKING_KINDS = frozenset({
    "unresolved_place", "unresolved_product", "unresolved_account",
    "ambiguous_account", "dropped_product", "dropped_account",
    "invented_product", "invented_account",
    "unhonoured_specialty",
    "threshold_direction_mismatch", "threshold_value_mismatch",
    "ranking_direction_mismatch",
})
