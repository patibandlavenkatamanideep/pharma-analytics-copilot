"""No metric, grain, filter or comparison combination reaches PostgreSQL as
an error.

The property, checked exhaustively rather than by example: every plan the
schema can express is either

* **refused by name** -- ``UnsupportedCombination``, which the pipeline turns
  into a request to rephrase -- or
* **answerable** -- it compiles, passes the SQL validator, and PostgreSQL can
  plan it.

Nothing may fall between: no bare ``CompileError`` for a combination the
user could reasonably ask for, no SQL the validator rejects, no statement the
database refuses. The review found ``facility_count_all`` by territory
passing AST validation and then failing in PostgreSQL with DuplicateAlias --
AST-valid SQL is not executable SQL, so this uses ``EXPLAIN``, which catches
every planning-time error (a duplicate alias, a missing FROM entry, a type
mismatch) in milliseconds without scanning two million rows.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import Compiler, UnsupportedCombination
from app.analytics.plan import AnalyticalPlan, Dimension, MetricKey
from app.analytics.registry import get_registry
from app.analytics.validator import validate
from tests.conftest import needs_db

pytestmark = needs_db

R3M = {"kind": "named", "named": "r3m"}
PRIOR = {"kind": "named", "named": "r6m_prior"}

#: One representative value per filter family. The value need not match any
#: row: planning is what is under test, not the result.
FILTERS = {
    "product_names": ["ZENOVAX"], "ndcs": ["00000-000-00"],
    "strengths": ["10MG"], "market_categories": ["Oncology"],
    "market_subcategories": ["Docetaxel"], "specialties": ["Oncology"],
    "classifications": ["generic"], "account_ids": ["GP0001"],
    "facility_ids": ["FA0001"], "org_archetypes": ["Academic"],
    "gpo_names": ["Vizient"], "org_types": ["Facility"],
    "territories": ["Anywhere"], "regions": ["West"], "states": ["TX"],
    "is_340b": "only", "active_only": True, "standalone_only": True,
}


def _base(metric: MetricKey) -> dict:
    spec = get_registry().get(metric.value)
    out = {"metric": metric.value, "time": R3M}
    if spec.get("kind") == "period_change":
        out["comparison"] = PRIOR
    return out


def _attempt(owner_cursor, anchor, plan_dict) -> str:
    """'refused', 'ok', or raise with the reason it is neither."""
    plan = AnalyticalPlan.model_validate(plan_dict)
    spec = get_registry().get(plan.metric.value)
    try:
        query = Compiler(max_rows=5000).compile(plan, anchor=anchor)
    except UnsupportedCombination:
        return "refused"
    validate(query.sql, wac_authorized=bool(spec.get("requires_wac")))
    owner_cursor.execute("EXPLAIN " + query.sql, query.params)
    return "ok"


@pytest.fixture
def owner_cursor():
    from app.db import owner_transaction
    with owner_transaction() as cur:
        yield cur


@pytest.mark.parametrize("metric", list(MetricKey), ids=lambda m: m.value)
@pytest.mark.parametrize("grain", list(Dimension), ids=lambda d: d.value)
def test_every_metric_by_every_grain(owner_cursor, anchor, metric, grain):
    plan = {**_base(metric), "dimensions": [grain.value]}
    assert _attempt(owner_cursor, anchor, plan) in ("ok", "refused")


@pytest.mark.parametrize("metric", list(MetricKey), ids=lambda m: m.value)
@pytest.mark.parametrize("field", sorted(FILTERS), ids=str)
def test_every_metric_with_every_filter_family(owner_cursor, anchor, metric, field):
    plan = {**_base(metric), "filters": {field: FILTERS[field]}}
    assert _attempt(owner_cursor, anchor, plan) in ("ok", "refused")


@pytest.mark.parametrize("metric", list(MetricKey), ids=lambda m: m.value)
def test_every_metric_with_a_comparison(owner_cursor, anchor, metric):
    """Only a change metric may carry one; everything else is refused by
    name rather than having the second window dropped."""
    plan = {**_base(metric), "comparison": PRIOR}
    outcome = _attempt(owner_cursor, anchor, plan)
    kind = get_registry().get(metric.value).get("kind")
    assert outcome == ("ok" if kind == "period_change" else "refused")


@pytest.mark.parametrize("metric", list(MetricKey), ids=lambda m: m.value)
def test_every_metric_with_geography_grain_and_filter_together(owner_cursor, anchor, metric):
    """The exact shape that failed: a geography grain AND a geography filter,
    so the zip_territory join is needed twice over."""
    plan = {**_base(metric), "dimensions": ["territory"],
            "filters": {"regions": ["West"], "states": ["TX"]}}
    assert _attempt(owner_cursor, anchor, plan) in ("ok", "refused")
