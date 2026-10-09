"""What a threshold phrase means, decided once.

"At least 100 packs" compiled to ``value > 100``, so an account that bought
exactly 100 disappeared from an answer that asked for it. The plan could not
say anything else: ``Threshold.direction`` was ``above | below``, and both
compiled to a strict comparison.

The operator is now explicit (``gt``, ``gte``, ``lt``, ``lte``), and the
table below is the one place that says which English phrase means which. The
offline planner reads questions with it, the live prompt is generated from it,
and the intent guard checks plans against it -- three consumers of one table,
so "at least" cannot mean ``>=`` to one of them and ``>`` to another.

A decline is a negative growth value, so the operator flips: "declined more
than 20%" is growth ``< -0.2``. "Declined LESS than 20%" is a range -- a fall,
but a small one -- which one bound cannot express; it is reported as needing
two bounds rather than approximated by one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Op = Literal["gt", "gte", "lt", "lte"]

SQL_OPERATOR: dict[str, str] = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

#: How each operator reads back to a user. Used in disclosures so a boundary
#: is visible: "at least 100" and "more than 100" differ by exactly the rows a
#: reader is most likely to check.
PHRASE_FOR: dict[str, str] = {
    "gt": "more than", "gte": "at least", "lt": "less than", "lte": "at most",
}

#: Leading phrases, each followed by the number. Longest alternatives first
#: within an operator, and matching picks the EARLIEST phrase in the question
#: -- so "no more than 5" is lte, not the "more than 5" inside it.
_LEADING: tuple[tuple[Op, str], ...] = (
    ("gte", r"at least|no less than|no fewer than|not less than|not fewer than|"
            r"a minimum of|minimum of"),
    ("lte", r"at most|no more than|no greater than|not more than|not greater than|"
            r"a maximum of|maximum of|up to"),
    ("gt", r"more than|greater than|higher than|larger than|in excess of|"
           r"exceeding|exceeds|over|above"),
    ("lt", r"less than|fewer than|lower than|smaller than|under|below"),
)

#: "100 or more", "20% or less".
_TRAILING: tuple[tuple[Op, str], ...] = (
    ("gte", r"or more|or greater|or higher|or above|or over"),
    ("lte", r"or less|or fewer|or lower|or below|or under"),
)

#: The \b after the digits stops backtracking into a shorter number: without
#: it, "over 30 months" fails the span check on 30, retries with 3, and finds
#: "0 months" is not a span.
_NUMBER = r"(\d[\d,]*(?:\.\d+)?)\b\s*(%|percent\b)?"

#: "over 3 months" is a time span, not a threshold of 3.
_NOT_A_SPAN = r"(?!\s*(?:months?|weeks?|quarters?|years?|days?|mos?\b|wks?\b))"

_DECLINE = re.compile(
    r"\b(?:declin\w*|drop\w*|fell|fall\w*|lost|losing|down|decreas\w*|"
    r"shrank|shrink\w*|contract\w*)\b", re.I)

_PATTERNS: list[tuple[Op, re.Pattern[str]]] = [
    (op, re.compile(rf"\b(?:{alts})\s+{_NUMBER}{_NOT_A_SPAN}", re.I))
    for op, alts in _LEADING
] + [
    (op, re.compile(rf"\b{_NUMBER}\s+(?:{alts})\b", re.I))
    for op, alts in _TRAILING
]

_FLIP: dict[str, str] = {"gt": "lt", "gte": "lte"}


@dataclass(frozen=True)
class ReadThreshold:
    """A threshold as the question states it, before metric units apply."""

    op: Op | None               # None when the phrase needs two bounds
    number: float               # as written: 20 for "20%", 500 for "500"
    percent: bool               # the number carried a % sign or "percent"
    decline: bool               # the question is about a fall
    phrase: str                 # the matched text, for disclosures

    @property
    def needs_range(self) -> bool:
        return self.op is None


def read_threshold(question: str) -> ReadThreshold | None:
    """The threshold a question states, or None if it states none."""
    best: tuple[int, int, Op, re.Match[str]] | None = None
    for op, pattern in _PATTERNS:
        for match in pattern.finditer(question or ""):
            # Earliest start wins; at the same start, the longer phrase wins.
            key = (match.start(), -(match.end() - match.start()))
            if best is None or key < best[:2]:
                best = (key[0], key[1], op, match)
    if best is None:
        return None

    _, _, op, match = best
    number = float(match.group(1).replace(",", ""))
    percent = bool(match.group(2))
    decline = bool(_DECLINE.search(question))

    final: Op | None = op
    if decline:
        # "declined more than 20%" is growth below -20%. "Declined less than
        # 20%" is a fall of under 20% -- a range, -0.2 < growth < 0, which a
        # single bound cannot express. Reporting it as one bound would include
        # every account that GREW, and call them small decliners.
        final = _FLIP.get(op)  # type: ignore[assignment]

    return ReadThreshold(op=final, number=number, percent=percent,
                         decline=decline, phrase=match.group(0).strip())


def same_direction(a: str, b: str) -> bool:
    """gt/gte are both lower bounds; lt/lte are both upper bounds."""
    return a[:2] == b[:2]


def prompt_guidance() -> str:
    """The threshold rules for the live model, generated from the table above
    so the prompt cannot drift from the parser."""
    def first(alts: str, n: int = 3) -> str:
        return ", ".join(f"'{a} N'" for a in alts.split("|")[:n])

    lines = ["THRESHOLDS -- the comparison operator is part of the question:"]
    for op, alts in _LEADING:
        included = "N included" if op.endswith("e") else "N excluded"
        lines.append(f"  {first(alts):<52} -> op {op:<3} ({included})")
    lines += [
        "  'N or more' -> gte;  'N or fewer' / 'N or less' -> lte",
        "  A decline is a NEGATIVE growth value: 'declined more than 20%' ->",
        "  op lt, value -0.2; 'declined at least 20%' -> op lte, value -0.2.",
        "  'Declined LESS than 20%' is a range that one threshold cannot express:",
        "  set clarification instead of choosing a single bound.",
        "  Values are in the metric's own units: a ratio takes 0.2 for 20%.",
    ]
    return "\n".join(lines)


def expected_value(read: ReadThreshold, unit: str) -> float | None:
    """The bound, in the metric's own units, that the question states.

    Driven by the unit the metric REGISTRY declares, not by a list of metric
    names: a list is what went stale in intent.py, so share_340b was told its
    percentage was "a count". Returns None when the question's number cannot
    be expressed in this metric's units at all -- "more than 20%" of a count
    of packs is not a bound on packs.

    * ratio -- a percentage: 20% and a bare 20 both mean 0.2.
    * percentage points -- the number as written: "up more than 2 points" is 2.
    * anything else (packs, equivalents, USD, accounts) -- the number as
      written, and a percent sign makes it inexpressible.
    """
    number = read.number
    if unit == "ratio":
        number /= 100.0
    elif unit == "percentage points":
        pass
    elif read.percent:
        return None
    return -abs(number) if read.decline else number
