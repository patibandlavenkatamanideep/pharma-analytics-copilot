"""What the question names, resolved before planning and checked after.

Review finding 2. The intent guard recognised an unknown product only when it
was typed in capitals: "FLOOBERTAX" was blocked, "Floobertax" and
"floobertax" produced an unfiltered plan and the whole company's volume under
the product's name. It never checked that a KNOWN product the question named
survived into the plan, and it knew nothing about accounts at all -- the
account resolvers in entities.py existed and were never called.

This module does three things, in order:

1. **Find mentions.** Products by case-insensitive match against the product
   vocabulary, longest first. Accounts against an index of top-level account
   names built from ``organizations`` under the CALLER'S row-level scope and
   cached per dataset and scope. The index is used here, on the server; it is
   never sent to the model. Only the ids of accounts the question actually
   names reach the planner.

2. **Find what could not be resolved.** A word in a product slot ("volume for
   X", "X sales") that is not in any vocabulary, in any casing. A Title-Case
   phrase in an account slot ("for X Y Z") that matches no account in scope.
   A name that matches several accounts -- the full dataset has 267 names
   shared by more than one organization -- is AMBIGUOUS, not the largest one.

3. **Check survival** (``fidelity_gaps``): every named product and account is
   in the plan's filters, and every product or account the plan filters on
   exists. A plan that drops a named entity answers a broader question under
   the narrow question's heading.

Out-of-scope accounts do not leak. The decision uses only the scoped index
and the question's syntax, so "not available to you" reads the same whether
the account exists elsewhere or nowhere.

What this deliberately does not do: match partial account names. "Prairie"
is not "Prairie Clinical Network"; fuzzy matching a restricted catalog would
turn a typo into someone else's account. Accounts are matched by full name,
and the refusal says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Literal

#: The version of the mention rules, for evidence records.
MENTIONS_VERSION = "1.0.0"

MentionKind = Literal["product", "account", "unknown_product", "unknown_account"]

#: Words that sit in a product or account slot without naming one.
_STOPWORDS = frozenset("""
    a an the our my their its this that these those each every all any some
    total overall combined company companies brand brands market markets
    account accounts facility facilities hospital hospitals clinic clinics
    product products territory territories region regions state states
    quarter quarters month months year years week weeks period periods day days
    today yesterday current prior previous last next latest recent same other
    q1 q2 q3 q4 ytd mtd qtd r3m r6m monthly weekly quarterly yearly annual
    january february march april may june july august september october
    november december jan feb mar apr jun jul aug sep sept oct nov dec
    average mean median highest lowest top bottom most least more less fewer
    paid free net gross unit units pack packs equivalent equivalents volume
    sales demand revenue share trend growth performance orders order dispense
    dispensed purchases purchase which what who how much many is are was were
    does did do be been have has had and or of in on at to by from with for
    versus vs against compared relative than excluding except without
    active inactive standalone generic generics biosimilar biosimilars branded
    competitor competitors 340b non non-340b pap wac hub distributor
    everything everyone anything nothing both either neither
    health system systems network networks group groups
""".split())

#: Common words that sit before "share", "volume" or "sales" as verbs and
#: adjectives -- "gained share", "strong sales". The slot "X share" read them
#: as product names. A word absent from here and from every vocabulary still
#: counts as an unknown name: a false alarm costs a clarification, a miss
#: costs a confident company-wide answer.
_COMMON = frozenset("""
    gain gained gaining gains lose lost losing loses grew grow growing grown
    grows increase increased increasing increases decrease decreased
    decreasing decreases decline declined declining declines drop dropped
    dropping drops rise rose rising rises fall fell falling falls raise
    raised boost boosted fastest slowest higher lower highest lowest biggest
    smallest bigger smaller large larger largest big small strong stronger
    strongest weak weaker weakest good better best bad worse worst new newer
    newest old older oldest recent early earlier late later high low
    steady stable flat relative absolute actual expected projected estimated
    reported whole entire full partial cumulative incremental organic
    underlying true real raw adjusted normalised normalized
    show shows showed see saw give gives gave get gets got
    compare compares comparing rank ranked ranks ranking list lists listed
    tell find
""".split())

#: Words that mean a phrase is a period or a quantifier, not an organisation.
#: Deliberately excludes words organisations are named with.
_NOT_ORGANISATION = frozenset("""
    the each every all any some our my their its this that these those
    total overall combined
    q1 q2 q3 q4 ytd mtd qtd r3m r6m quarter quarters month months year years
    week weeks january february march april may june july august september
    october november december jan feb mar apr jun jul aug sep sept oct nov dec
    last next prior previous current this
""".split())

_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}

#: A reference, not a filter: "gaining share against ZENOVAX" names a product
#: the answer is about something other than.
_REFERENCE_CUE = re.compile(
    r"\b(?:versus|vs\.?|against|compared (?:to|with)|relative to|other than|"
    r"excluding|except|without|instead of)\s*$", re.I)

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'.-]*")

#: "volume for X this quarter", "sales of X in Q3", "X volume".
_PRODUCT_SLOTS = (
    re.compile(
        r"\b(?:volume|sales|units|packs|equivalents|demand|share|revenue|"
        r"dispenses|purchases|orders|performance|trend)\s+(?:for|of|on)\s+"
        r"([A-Za-z][A-Za-z0-9-]{3,})\b", re.I),
    re.compile(
        r"\b([A-Za-z][A-Za-z0-9-]{3,})\s+(?:volume|sales|units|packs|"
        r"equivalents|demand|share|revenue|dispenses|purchases)\b", re.I),
    re.compile(
        r"\bhow (?:is|are|was|were|did|does)\s+([A-Za-z][A-Za-z0-9-]{3,})\s+"
        r"(?:doing|performing|selling|trending)\b", re.I),
)

#: "for Northwind Regional Health", "at St. Brigid Medical Center".
_ACCOUNT_SLOT = re.compile(
    r"\b(?:for|at|from)\s+(?:the\s+)?"
    r"((?:[A-Z][\w'&.-]*\s+){1,5}[A-Z][\w'&.-]*)")


def normalise(text: str) -> str:
    """Case, whitespace and punctuation-insensitive form of a name.

    Apostrophes are dropped, not the letters after them: "St. Mary's" and
    "St Marys" are the same name. A possessive on a product ("ZENOVAX's") is
    handled at lookup, by also trying the form without it -- stripping "'s"
    here turned "Mary's" into "Mary" and missed a label stored as "Marys".
    """
    text = text.casefold().replace("\u2019", "'").replace("&", " and ").replace("'", "")
    return " ".join(re.sub(r"[^\w-]+", " ", text).split()).strip("-")


def _lookup_keys(text: str) -> tuple[str, ...]:
    """The forms under which a span of the question may match a name."""
    plain = normalise(text)
    without_possessive = normalise(re.sub(r"['\u2019]s\b", "", text))
    return (plain,) if plain == without_possessive else (plain, without_possessive)


@dataclass(frozen=True)
class AccountRef:
    entity_id: str
    label: str
    detail: str


@dataclass(frozen=True)
class EntityIndex:
    """Names the caller may refer to, by normalised form. Server-side only."""

    products: dict[str, str] = field(default_factory=dict)          # norm -> name
    accounts: dict[str, tuple[AccountRef, ...]] = field(default_factory=dict)
    known_words: frozenset[str] = frozenset()                        # other vocab
    #: Whether accounts were loaded. Without them, an unmatched phrase in an
    #: account slot says nothing -- there was nothing to match it against.
    has_accounts: bool = False

    @property
    def account_ids(self) -> frozenset[str]:
        return frozenset(r.entity_id for refs in self.accounts.values() for r in refs)


@dataclass(frozen=True)
class Mention:
    kind: MentionKind
    text: str
    start: int
    end: int
    #: product: (canonical name,); account: every candidate id
    ids: tuple[str, ...] = ()
    candidates: tuple[AccountRef, ...] = ()
    #: named as a point of comparison, not as the population
    reference_only: bool = False

    @property
    def ambiguous(self) -> bool:
        return self.kind == "account" and len(self.ids) > 1


def index_from(
    products: Iterable[str],
    accounts: Iterable[AccountRef] | None = None,
    other_vocabulary: Iterable[str] = (),
) -> EntityIndex:
    """Build an index from plain values. The database-backed builder below
    uses this; tests build one directly."""
    has_accounts = accounts is not None
    by_label: dict[str, list[AccountRef]] = {}
    for ref in accounts or ():
        by_label.setdefault(normalise(ref.label), []).append(ref)
    known = set()
    for value in other_vocabulary:
        norm = normalise(value)
        known.add(norm)
        known.update(norm.split())
    for state, code in _STATES.items():
        known.add(state)
        known.update(state.split())
        known.add(code.lower())
    return EntityIndex(
        products={normalise(p): p for p in products if p},
        # Candidates in a fixed order. They are shown to the user and stored,
        # and "the second one" must mean the same account every time; the
        # database returns grouped rows in no particular order.
        accounts={k: tuple(sorted(v, key=lambda r: (r.detail, r.entity_id)))
                  for k, v in by_label.items()},
        known_words=frozenset(known),
        has_accounts=has_accounts,
    )


def find_mentions(question: str, index: EntityIndex) -> list[Mention]:
    """Every entity the question names, resolved or not, left to right."""
    words = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(question or "")]
    found: list[Mention] = []
    taken: set[int] = set()

    # --- known names, longest first ------------------------------------------
    i = 0
    while i < len(words):
        match = None
        for n in range(min(8, len(words) - i), 0, -1):
            span = words[i:i + n]
            for key in _lookup_keys(" ".join(w for w, _, _ in span)):
                if key in index.products:
                    match = ("product", n, (index.products[key],), ())
                elif key in index.accounts:
                    refs = index.accounts[key]
                    match = ("account", n, tuple(r.entity_id for r in refs), refs)
                if match:
                    break
            if match:
                break
        if match:
            kind, n, ids, refs = match
            start, end = words[i][1], words[i + n - 1][2]
            found.append(Mention(
                kind=kind, text=question[start:end], start=start, end=end,
                ids=ids, candidates=refs,
                reference_only=bool(_REFERENCE_CUE.search(question[:start]))))
            taken.update(range(start, end))
            i += n
        else:
            i += 1

    def free(start: int, end: int) -> bool:
        return not (set(range(start, end)) & taken)

    def is_known(text: str) -> bool:
        norm = normalise(text)
        return (norm in _STOPWORDS or norm in _COMMON or norm in index.known_words
                or norm in index.products or norm.isdigit())

    def not_an_organisation(word: str) -> bool:
        norm = normalise(word)
        return (norm in _NOT_ORGANISATION or norm in index.known_words
                or norm in index.products or norm.isdigit())

    # --- unknown accounts, in an account slot --------------------------------
    # Before the product slots: a multi-word Title-Case name is an
    # organisation, and the one-word product slot would otherwise claim its
    # first word -- "Memorial is not a product" -- for a real account the
    # caller simply cannot see.
    for m in _ACCOUNT_SLOT.finditer(question or "") if index.has_accounts else ():
        text = m.group(1).rstrip(".,?")
        start, end = m.start(1), m.start(1) + len(text)
        if not free(start, end):
            continue
        # A place, a product, a period or a determiner makes this something
        # other than an organisation: "for New York", "for Q3 2026", "for Each
        # Territory". Words that are merely COMMON in organisation names --
        # Health, Network, Clinic -- do not: the first version disqualified
        # on the general stopword list, so "Northwind Regional Health" was
        # never reported as unknown.
        if any(not_an_organisation(w) for w in text.split()):
            continue
        found.append(Mention(kind="unknown_account", text=text, start=start, end=end))
        taken.update(range(start, end))

    # --- unknown products, in any casing -------------------------------------
    for pattern in _PRODUCT_SLOTS:
        for m in pattern.finditer(question or ""):
            text, start, end = m.group(1), m.start(1), m.end(1)
            if not free(start, end) or is_known(text):
                continue
            found.append(Mention(kind="unknown_product", text=text, start=start, end=end))
            taken.update(range(start, end))

    return sorted(found, key=lambda x: x.start)


def apply_choices(mentions: list[Mention], resolved: dict[str, str] | None) -> list[Mention]:
    """Resolve an ambiguous mention with the option the user chose.

    `resolved` maps a normalised phrase to the chosen entity id. The choice is
    applied only if that id is STILL a candidate for the phrase in the
    caller's current index -- a stored option is not a grant, and access can
    change between a question and its answer.
    """
    if not resolved:
        return mentions
    out = []
    for m in mentions:
        pick = resolved.get(normalise(m.text))
        if m.ambiguous and pick in m.ids:
            m = Mention(kind=m.kind, text=m.text, start=m.start, end=m.end,
                        ids=(pick,), candidates=tuple(c for c in m.candidates
                                                      if c.entity_id == pick),
                        reference_only=m.reference_only)
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# The database-backed index
# ---------------------------------------------------------------------------

def entity_index(principal, dataset_id: str, vocabulary) -> EntityIndex:
    """The caller's index, built under their row-level scope.

    Cached per (dataset, scope kind, scope value): a Director's index must
    never answer an RAM's question, and a reload must not serve the previous
    snapshot's names.
    """
    accounts = _scoped_accounts(dataset_id, principal.scope_kind,
                                principal.scope_value or "")
    other = (list(vocabulary.subcategories) + list(vocabulary.categories)
             + list(vocabulary.specialties) + list(vocabulary.gpos)
             + list(vocabulary.archetypes) + list(vocabulary.all_territories)
             + list(vocabulary.all_regions) + list(vocabulary.company_names))
    return index_from(vocabulary.products, accounts, other)


@lru_cache(maxsize=64)
def _scoped_accounts(dataset_id: str, scope_kind: str, scope_value: str) -> tuple[AccountRef, ...]:
    from app.db import analytics_transaction

    with analytics_transaction(scope_kind=scope_kind, scope_value=scope_value or None,
                               wac_authorized=False) as cur:
        cur.execute(
            """
            SELECT COALESCE(o.grandparent_org_id, o.org_id)     AS entity_id,
                   COALESCE(o.grandparent_org_name, o.org_name) AS label,
                   count(DISTINCT o.org_id)                     AS facilities,
                   min(o.state) AS a_state, max(o.state) AS b_state
            FROM organizations o
            WHERE COALESCE(o.grandparent_org_name, o.org_name) IS NOT NULL
            GROUP BY 1, 2
            """)
        rows = cur.fetchall()
    out = []
    for r in rows:
        states = r["a_state"] if r["a_state"] == r["b_state"] else f"{r['a_state']}–{r['b_state']}"
        n = r["facilities"]
        out.append(AccountRef(entity_id=r["entity_id"], label=r["label"],
                              detail=f"{n} facilit{'y' if n == 1 else 'ies'}, {states}"))
    return tuple(out)


def clear_caches() -> None:
    _scoped_accounts.cache_clear()
