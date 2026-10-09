"""The shape a question asks for: what it is broken down by, and whether it
is a ranking.

"Top 20 facilities by paid pack units" answered with one company-wide total
is a true number and the wrong answer, and nothing downstream could tell:
the plan was valid, compiled cleanly, and returned a confident figure. Read
from the question here, and compared with the plan in app.analytics.intent,
for every planner -- the live model as much as the offline one.

Deliberately narrow. Only phrases that unambiguously name a grouping are
read: "by territory", "for each product", "monthly", "top 10 accounts",
"which facilities had the most". "Per" is not read -- "average volume per
facility" is a ratio, not a breakdown -- and neither is "across".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.analytics.plan import Dimension

#: word pattern -> the grains that satisfy it, the first being the default.
_WORDS: list[tuple[str, tuple[Dimension, ...]]] = [
    (r"facilit(?:y|ies)|clinics?|hospitals?|sites?", (Dimension.facility,)),
    (r"accounts?|health[- ]systems?", (Dimension.account, Dimension.parent)),
    (r"parent(?: organi[sz]ation)?s?", (Dimension.parent, Dimension.account)),
    (r"products?|brands?|drugs?", (Dimension.product, Dimension.ndc)),
    (r"ndcs?", (Dimension.ndc,)),
    (r"territor(?:y|ies)", (Dimension.territory,)),
    (r"regions?", (Dimension.region,)),
    (r"states?", (Dimension.state,)),
    (r"gpos?", (Dimension.gpo,)),
    (r"specialt(?:y|ies)|therapeutic areas?", (Dimension.specialty,)),
    (r"archetypes?", (Dimension.archetype,)),
    (r"(?:market )?subcategor(?:y|ies)", (Dimension.market_subcategory,)),
    (r"(?:market )?categor(?:y|ies)", (Dimension.market_category,)),
    (r"(?:data )?sources?", (Dimension.data_source,)),
    (r"340b(?: status)?", (Dimension.is_340b,)),
    (r"months?", (Dimension.period_mo,)),
    (r"quarters?", (Dimension.period_qtr,)),
    (r"weeks?", (Dimension.period_wk,)),
]
_WORD = "|".join(f"(?:{w})" for w, _ in _WORDS)
_LIST = rf"(?:{_WORD})(?:\s*(?:,|and|&|\+|then)\s*(?:{_WORD}))*"

_GROUPING = re.compile(
    rf"\b(?:by|for each|for every|each|every|broken down by|split by|grouped by|"
    rf"breakdown by)\s+(?:the\s+)?({_LIST})\b", re.I)
_ADVERB = re.compile(r"\b(monthly|quarterly|weekly|month[- ]over[- ]month|"
                     r"quarter[- ]over[- ]quarter|week[- ]over[- ]week)\b", re.I)
_ADVERB_GRAIN = {"month": Dimension.period_mo, "quarter": Dimension.period_qtr,
                 "week": Dimension.period_wk}

_NUMBER_WORDS = {"three": 3, "five": 5, "ten": 10, "twenty": 20, "fifty": 50}
# Superlatives that rank whatever follows them. "Most" and "least" are not
# here: "most accounts grew" is not a ranking. They count only after
# "which X", where the ranked entity is named.
_LOW = r"bottom|lowest|smallest|worst"
_HIGH = r"top|highest|largest|biggest|best"
_LOW_WHICH = _LOW + r"|least|fewest"
_HIGH_WHICH = _HIGH + r"|most"
_RANKED = re.compile(
    rf"\b({_HIGH}|{_LOW})\s+(?:(\d{{1,3}}|three|five|ten|twenty|fifty)\s+)?"
    rf"(?:[a-z0-9-]+\s+){{0,2}}?({_WORD})\b", re.I)
_WHICH = re.compile(
    rf"\bwhich\s+({_WORD})\b[^.?;]*?\b(?:the\s+)?({_HIGH_WHICH}|{_LOW_WHICH})\b", re.I)


def grains_for(word: str) -> tuple[Dimension, ...]:
    for pattern, grains in _WORDS:
        if re.fullmatch(pattern, word.strip(), re.I):
            return grains
    return ()


@dataclass(frozen=True)
class AskedRanking:
    word: str
    grains: tuple[Dimension, ...]
    direction: str                 # "top" or "bottom"
    limit: int | None              # None: "which ... the most", no number


@dataclass(frozen=True)
class AskedShape:
    groupings: tuple[tuple[str, tuple[Dimension, ...]], ...]
    ranking: AskedRanking | None


def asked_shape(question: str) -> AskedShape:
    q = question.lower()
    groupings: list[tuple[str, tuple[Dimension, ...]]] = []
    for m in _GROUPING.finditer(q):
        for word in re.split(r"\s*(?:,|and|&|\+|then)\s*", m.group(1)):
            if word and (grains := grains_for(word)):
                groupings.append((word, grains))
    for m in _ADVERB.finditer(q):
        unit = next(u for u in _ADVERB_GRAIN if u in m.group(1))
        groupings.append((m.group(1), (_ADVERB_GRAIN[unit],)))

    ranking = None
    # "Which health systems have the most facilities": the ranked entity is
    # the one "which" names, not the one after the superlative.
    if m := _WHICH.search(q):
        word, word_dir = m.group(1), m.group(2)
        ranking = AskedRanking(word=word, grains=grains_for(word),
                               direction="bottom" if re.fullmatch(_LOW_WHICH, word_dir)
                               else "top",
                               limit=None)
    elif m := _RANKED.search(q):
        word_dir, count, word = m.group(1), m.group(2), m.group(3)
        # "most recent month", "top of the hour": a time word after a
        # superlative is not a ranking of periods unless a number is given.
        if not (grains_for(word)[0] in (Dimension.period_mo, Dimension.period_qtr,
                                         Dimension.period_wk) and not count):
            ranking = AskedRanking(
                word=word, grains=grains_for(word),
                direction="bottom" if re.fullmatch(_LOW, word_dir) else "top",
                limit=(int(count) if count and count.isdigit()
                       else _NUMBER_WORDS.get(count or "", None)))
    # A grouping that is the ranked entity is the same request, said twice.
    if ranking is not None:
        groupings = [(w, g) for w, g in groupings if g != ranking.grains]
    return AskedShape(groupings=tuple(groupings), ranking=ranking)
