"""Entity resolution.

Two jobs:

  * Supply the planner with the vocabularies it may choose from, already
    narrowed to what the principal can see. Offering a RAM the names of other
    territories invites a plan that will only be refused later.

  * Turn a name the user typed into stable IDs. Names are never used as keys:
    the full dataset contains 267 organization-name groups shared by more than
    one org_id, so matching on a name would silently merge distinct systems.
    An ambiguous term returns candidates for the user to choose between rather
    than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.auth.policy import Principal
from app.db import analytics_transaction


@dataclass
class Vocabulary:
    products: list[str] = field(default_factory=list)
    subcategories: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    gpos: list[str] = field(default_factory=list)
    archetypes: list[str] = field(default_factory=list)
    territories: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    entity_id: str
    label: str
    detail: str


@lru_cache(maxsize=1)
def _product_vocabulary() -> tuple[list[str], list[str], list[str]]:
    """Products are unrestricted reference data, so this is cached globally."""
    with analytics_transaction(
        scope_kind="global", scope_value=None, wac_authorized=False
    ) as cur:
        cur.execute("SELECT DISTINCT drug_name FROM products WHERE brand_flag = 1 ORDER BY 1")
        company = [r["drug_name"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT drug_name FROM products WHERE brand_flag = 0 ORDER BY 1")
        competitors = [r["drug_name"] for r in cur.fetchall()]
        cur.execute(
            "SELECT DISTINCT market_subcategory FROM products "
            "WHERE market_subcategory IS NOT NULL ORDER BY 1"
        )
        subs = [r["market_subcategory"] for r in cur.fetchall()]
        cur.execute(
            "SELECT DISTINCT market_category FROM products "
            "WHERE market_category IS NOT NULL ORDER BY 1"
        )
        cats = [r["market_category"] for r in cur.fetchall()]
    return company + competitors, subs, cats


def vocabulary_for(principal: Principal) -> Vocabulary:
    products, subs, cats = _product_vocabulary()

    # zip_territory is unrestricted reference data, but the planner is still
    # only offered the geography the principal can act on: suggesting a
    # territory that authorization will refuse produces a worse answer.
    if principal.role == "exec":
        territory_sql = "SELECT DISTINCT territory_name FROM zip_territory ORDER BY 1"
        territory_params: tuple[Any, ...] = ()
        region_sql = (
            "SELECT DISTINCT region_name FROM zip_territory "
            "WHERE region_name IS NOT NULL ORDER BY 1"
        )
        region_params: tuple[Any, ...] = ()
    elif principal.role == "director":
        territory_sql = (
            "SELECT DISTINCT territory_name FROM zip_territory WHERE region_name = %s ORDER BY 1"
        )
        territory_params = (principal.region_name,)
        region_sql = "SELECT %s AS region_name"
        region_params = (principal.region_name,)
    else:
        territory_sql = "SELECT %s AS territory_name"
        territory_params = (principal.territory_name,)
        region_sql = "SELECT NULL AS region_name WHERE false"
        region_params = ()

    with analytics_transaction(
        scope_kind=principal.scope_kind,
        scope_value=principal.scope_value,
        wac_authorized=False,
    ) as cur:
        cur.execute(territory_sql, territory_params)
        territories = [r["territory_name"] for r in cur.fetchall() if r["territory_name"]]
        cur.execute(region_sql, region_params)
        regions = [r["region_name"] for r in cur.fetchall() if r["region_name"]]
        # These come from organizations, so RLS already limits them to scope.
        cur.execute(
            "SELECT DISTINCT gpo_name FROM organizations WHERE gpo_name IS NOT NULL ORDER BY 1"
        )
        gpos = [r["gpo_name"] for r in cur.fetchall()]
        cur.execute(
            "SELECT DISTINCT org_archetype FROM organizations "
            "WHERE org_archetype IS NOT NULL ORDER BY 1"
        )
        archetypes = [r["org_archetype"] for r in cur.fetchall()]

    return Vocabulary(
        products=products, subcategories=subs, categories=cats,
        gpos=gpos, archetypes=archetypes, territories=territories, regions=regions,
    )


def resolve_accounts(term: str, principal: Principal, *, limit: int = 8) -> list[Candidate]:
    """Find top-level accounts matching a name, within the principal's scope.

    RLS restricts the rows, so a Director searching a name cannot discover a
    system that exists only outside their region.
    """
    pattern = f"%{term.strip()}%"
    with analytics_transaction(
        scope_kind=principal.scope_kind,
        scope_value=principal.scope_value,
        wac_authorized=False,
    ) as cur:
        cur.execute(
            """
            SELECT COALESCE(o.grandparent_org_id, o.org_id)   AS entity_id,
                   COALESCE(o.grandparent_org_name, o.org_name) AS label,
                   count(DISTINCT o.org_id)                   AS facilities,
                   min(o.state)                               AS a_state,
                   max(o.state)                               AS b_state
            FROM organizations o
            WHERE COALESCE(o.grandparent_org_name, o.org_name) ILIKE %s
            GROUP BY 1, 2
            ORDER BY facilities DESC, label
            LIMIT %s
            """,
            (pattern, limit),
        )
        rows = cur.fetchall()

    out: list[Candidate] = []
    for row in rows:
        states = row["a_state"] if row["a_state"] == row["b_state"] else \
            f"{row['a_state']}–{row['b_state']}"
        out.append(
            Candidate(
                entity_id=row["entity_id"],
                label=row["label"],
                # Enough authorized context to tell two same-named systems apart.
                detail=f"{row['facilities']} facilit{'y' if row['facilities'] == 1 else 'ies'}, {states}",
            )
        )
    return out


def resolve_products(term: str) -> list[Candidate]:
    pattern = f"%{term.strip()}%"
    with analytics_transaction(
        scope_kind="global", scope_value=None, wac_authorized=False
    ) as cur:
        cur.execute(
            """
            SELECT drug_name, market_subcategory, brand_flag,
                   count(*) AS ndcs, string_agg(DISTINCT strength, ', ') AS strengths
            FROM products
            WHERE drug_name ILIKE %s OR generic_name ILIKE %s
            GROUP BY drug_name, market_subcategory, brand_flag
            ORDER BY brand_flag DESC, drug_name
            LIMIT 10
            """,
            (pattern, pattern),
        )
        rows = cur.fetchall()
    return [
        Candidate(
            entity_id=r["drug_name"],
            label=r["drug_name"],
            detail=(
                f"{'our brand' if r['brand_flag'] == 1 else 'competitor'}, "
                f"{r['market_subcategory']} market, {r['strengths']}"
            ),
        )
        for r in rows
    ]


def clear_caches() -> None:
    _product_vocabulary.cache_clear()
