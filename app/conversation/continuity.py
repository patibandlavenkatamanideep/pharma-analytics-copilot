"""What carries from one turn to the next, decided in one place.

This module exists because the same question was being answered twice, in two
places, with two different answers.

`OfflinePlanner` learned typed cohorts: a cohort of products goes into
`product_names`, a cohort of accounts into `account_ids`, and a period is not
a cohort at all. The prompt sent to the live model learned none of that. It
said, for every cohort of every kind:

    The previous answer was about these account ids: ZENOVAX, GEMTARA

and, for every turn that had any predecessor:

    This is a FOLLOW-UP.

So the deterministic planner used for every test did one thing and the model
that actually serves users did another, and no test could see the difference
because no test exercised the prompt.

Continuity is therefore resolved here, provider-independently, and both
planners consume the result. Three things this module is careful about:

* **A turn is classified, not assumed.** A question that shares a
  conversation with a previous answer is not automatically a follow-up. It
  may be a fresh question, a correction, an answer to a clarification, or an
  ambiguous reference that should be asked about rather than guessed at.
* **A cohort is typed, and knows whether it is complete.** The pipeline
  records at most 200 ids. A truncated cohort presented as "the previous
  result" is a quietly different population, so completeness travels with it
  and an incomplete cohort is disclosed rather than silently frozen.
* **A period is not a population.** "Those same months" is a time window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Bumped when the meaning of continuity changes, so an evidence record can
#: say which rules produced a plan.
CONTINUITY_VERSION = "1.0.0"

#: The plan filter each cohort grain belongs in. A grain absent from this map
#: has no population to carry -- a period is the obvious case: "those same
#: months" is a time window, not a set of entities.
COHORT_FILTER_FIELD: dict[str, str] = {
    "account": "account_ids",
    "facility": "facility_ids",
    "product": "product_names",
    "gpo": "gpo_names",
    "archetype": "org_archetypes",
    "territory": "territories",
    "region": "regions",
}

#: Filters that describe a POPULATION. A fresh question must not inherit
#: these; carrying them is how "revenue this quarter" silently became
#: "revenue this quarter for the three accounts we were just discussing".
POPULATION_FILTERS = (
    "account_ids", "facility_ids", "product_names", "ndcs", "strengths",
    "gpo_names", "org_archetypes", "org_types", "specialties",
    "classifications", "market_categories", "market_subcategories",
    "territories", "regions", "states",
)


class TurnKind(StrEnum):
    """What this turn is, relative to the one before it."""

    FRESH_QUESTION = "fresh_question"
    FOLLOW_UP = "follow_up"
    CORRECTION = "correction"
    CLARIFICATION_ANSWER = "clarification_answer"
    AMBIGUOUS_CONTINUATION = "ambiguous_continuation"


@dataclass(frozen=True)
class Cohort:
    """A set of entity ids at a known grain, from a known turn.

    `complete` is the field that matters. The pipeline stores at most 200
    ids; a cohort that was cut off is not the previous result population, and
    freezing it would answer a question about a subset while looking like it
    answered one about the whole.
    """

    dimension: str
    ids: tuple[str, ...]
    source_turn: int | None = None
    dataset_id: str | None = None
    complete: bool = True
    total_available: int | None = None

    @property
    def filter_field(self) -> str | None:
        """The plan filter these ids belong in, or None if they are not a
        population at all."""
        return COHORT_FILTER_FIELD.get(self.dimension)

    @property
    def is_population(self) -> bool:
        return self.filter_field is not None

    def describe(self) -> str:
        noun = {
            "account": "accounts", "facility": "facilities",
            "product": "products", "gpo": "GPOs",
            "archetype": "organization archetypes",
            "territory": "territories", "region": "regions",
        }.get(self.dimension, f"{self.dimension} values")
        if self.complete:
            return f"{len(self.ids)} {noun}"
        total = f"{self.total_available:,}" if self.total_available else "more"
        return f"{len(self.ids)} of {total} {noun} (truncated)"


@dataclass(frozen=True)
class Continuity:
    """The decision: what this turn is, and what it may carry."""

    kind: TurnKind
    previous_plan: dict[str, Any] | None = None
    cohort: Cohort | None = None
    #: Set when the turn cannot be resolved without asking.
    clarification: str | None = None
    #: Plain-language notes the answer must show, so an inherited filter is
    #: never silently applied.
    disclosures: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default=())

    @property
    def carries_previous_plan(self) -> bool:
        return self.kind in (TurnKind.FOLLOW_UP, TurnKind.CORRECTION,
                             TurnKind.CLARIFICATION_ANSWER)

    @property
    def carries_cohort(self) -> bool:
        """A cohort is frozen only on an explicit reference to it."""
        return (
            self.kind is TurnKind.FOLLOW_UP
            and self.cohort is not None
            and self.cohort.is_population
            and self.cohort.complete
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# An explicit reference back to the previous population.
_REFERS_BACK = re.compile(
    r"\b(?:those|these|the same|same ones?|that group|them)\b", re.I)

# A modification of the previous question rather than a new one.
_MODIFIES = re.compile(
    r"^\s*(?:and |but |also |now |then )?"
    r"(?:break (?:that|it) down|split (?:that|it)|group (?:that|it)|"
    r"exclude|include|only|add|remove|drop|filter|narrow|widen|"
    r"what about|how about|compare (?:that|it)|by \w+)\b", re.I)

# Correcting the previous turn.
_CORRECTS = re.compile(
    r"\b(?:no,|not |i meant|actually|instead|rather than|sorry,|"
    r"that'?s (?:wrong|not right)|should be)\b", re.I)

# A question that stands on its own: it names its own metric and subject.
_SELF_CONTAINED = re.compile(
    r"\b(?:what (?:is|are|was|were)|how (?:much|many)|show me|list|give me|"
    r"which|rank|compare)\b", re.I)

# A bare reference with nothing to anchor it.
_BARE_REFERENCE = re.compile(
    r"^\s*(?:what about|how about|and)?\s*(?:those|these|them|it|that)\s*\??\s*$",
    re.I)


def classify_turn(
    question: str,
    *,
    previous_plan: dict[str, Any] | None,
    awaiting_clarification: bool = False,
) -> tuple[TurnKind, tuple[str, ...]]:
    """Decide what this turn is. Returns the kind and why.

    Deliberately conservative: when a question reads as self-contained it is
    treated as fresh, because inheriting a population into a question that
    did not ask for one produces a confident answer about the wrong rows.
    """
    q = (question or "").strip()

    if awaiting_clarification:
        return TurnKind.CLARIFICATION_ANSWER, ("the previous turn asked a question",)

    if previous_plan is None:
        return TurnKind.FRESH_QUESTION, ("no previous plan in this conversation",)

    if _BARE_REFERENCE.match(q):
        return TurnKind.AMBIGUOUS_CONTINUATION, (
            "the question is a bare reference with nothing to anchor it",)

    if _CORRECTS.search(q):
        return TurnKind.CORRECTION, ("the question corrects the previous turn",)

    refers_back = bool(_REFERS_BACK.search(q))
    modifies = bool(_MODIFIES.match(q))

    if refers_back or modifies:
        return TurnKind.FOLLOW_UP, (
            "explicit reference to the previous result" if refers_back
            else "the question modifies the previous request",)

    if _SELF_CONTAINED.search(q):
        return TurnKind.FRESH_QUESTION, (
            "the question names its own metric and subject",)

    # Short, no self-contained shape, no explicit reference: it probably
    # continues something, but which part is not determinable.
    if len(q.split()) <= 4:
        return TurnKind.AMBIGUOUS_CONTINUATION, (
            "too short to stand alone and no explicit reference",)

    return TurnKind.FRESH_QUESTION, ("no continuation marker found",)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(
    question: str,
    *,
    previous_plan: dict[str, Any] | None = None,
    cohort: Cohort | None = None,
    awaiting_clarification: bool = False,
) -> Continuity:
    """Decide what this turn carries forward, and what must be disclosed."""
    kind, reasons = classify_turn(
        question, previous_plan=previous_plan,
        awaiting_clarification=awaiting_clarification)

    disclosures: list[str] = []
    clarification: str | None = None

    if kind is TurnKind.AMBIGUOUS_CONTINUATION:
        if cohort is not None and cohort.is_population:
            clarification = (
                f"Did you mean the {cohort.describe()} from the previous "
                f"answer, or something else? Naming them, or asking the "
                f"question in full, will resolve it."
            )
        else:
            clarification = (
                "That could continue the previous question or start a new "
                "one. Asking it in full will resolve it."
            )
        return Continuity(kind=kind, previous_plan=None, cohort=None,
                          clarification=clarification, reasons=reasons)

    carried_plan = previous_plan if kind in (
        TurnKind.FOLLOW_UP, TurnKind.CORRECTION,
        TurnKind.CLARIFICATION_ANSWER) else None

    carried_cohort: Cohort | None = None
    if kind is TurnKind.FOLLOW_UP and cohort is not None:
        if not cohort.is_population:
            # A period is not a population. Nothing to carry, nothing to say.
            pass
        elif not cohort.complete:
            # The stored cohort was cut off. Freezing it would answer about a
            # subset while looking like the whole.
            clarification = (
                f"The previous answer covered more rows than were kept "
                f"({cohort.describe()}), so 'those' cannot be resolved "
                f"exactly. Narrowing the question, or naming the ones you "
                f"mean, will resolve it."
            )
            return Continuity(kind=kind, previous_plan=carried_plan,
                              cohort=None, clarification=clarification,
                              reasons=reasons + ("cohort was truncated",))
        else:
            carried_cohort = cohort
            disclosures.append(
                f"Still looking at the {cohort.describe()} from your previous "
                f"question."
            )

    if carried_plan is not None and carried_cohort is None:
        inherited = _describe_inherited(carried_plan)
        if inherited:
            disclosures.append(f"Still applying: {inherited}.")

    return Continuity(
        kind=kind, previous_plan=carried_plan, cohort=carried_cohort,
        clarification=None, disclosures=tuple(disclosures), reasons=reasons)


def _describe_inherited(plan: dict[str, Any]) -> str:
    """Name the population filters a continuation keeps, for disclosure."""
    filters = (plan or {}).get("filters") or {}
    bits: list[str] = []
    for field_name in ("product_names", "market_categories",
                       "market_subcategories", "gpo_names", "org_archetypes",
                       "territories", "regions", "specialties"):
        values = filters.get(field_name) or []
        if values:
            bits.append(", ".join(str(v) for v in values[:4]))
    if filters.get("is_340b") and filters["is_340b"] != "include":
        bits.append("340B accounts only" if filters["is_340b"] == "only"
                    else "340B accounts excluded")
    if filters.get("active_only"):
        bits.append("active organizations only")
    return "; ".join(bits)


def clear_population_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Strip carried-over population filters for a fresh question.

    A fresh question inherits the conversation, not its population.
    """
    out = dict(filters)
    for field_name in POPULATION_FILTERS:
        if field_name in out:
            out[field_name] = []
    out["is_340b"] = "include"
    out["active_only"] = False
    out["standalone_only"] = False
    return out
