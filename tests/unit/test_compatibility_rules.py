"""Which metric can be combined with what, decided in one place.

Before `check_compatibility` existed, three unsupported plans met three
different fates depending on which code path noticed first:

* a structural count by territory failed inside PostgreSQL with
  ``DuplicateAlias`` -- the geography join pulled ``organizations`` back in
  after the structural branch had made it the FROM table;
* a structural count by month failed with a missing FROM entry for ``s``;
* a comparison window on a single-window metric was **silently dropped**, so
  "share this quarter compared with last" came back as one quarter.

The first two surfaced as errors. The third surfaced as a confident, wrong
answer, which is worse. These tests pin the rules without a database; the
integration matrix proves no combination escapes them.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import (
    REACHABLE_FROM,
    check_compatibility,
    reachable,
)
from app.analytics.plan import AnalyticalPlan, Dimension
from app.analytics.registry import get_registry

R3M = {"kind": "named", "named": "r3m"}
PRIOR = {"kind": "named", "named": "r6m_prior"}


def plan(**kw) -> AnalyticalPlan:
    base = {"metric": "paid_pack_units", "time": R3M}
    base.update(kw)
    return AnalyticalPlan.model_validate(base)


def reasons(p: AnalyticalPlan) -> list[str]:
    return check_compatibility(p, get_registry().get(p.metric.value))


# ---------------------------------------------------------------------------
# Comparison windows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("metric", [
    "paid_pack_units", "wac_revenue", "brand_market_share", "share_340b",
    "account_count", "facility_count_all",
])
def test_a_comparison_on_a_single_window_metric_is_refused_not_dropped(metric):
    """The silent case. The compiler never read plan.comparison for these
    kinds, so the second window simply vanished from the answer."""
    found = reasons(plan(metric=metric, comparison=PRIOR))
    assert any("comparison" in r for r in found), found


@pytest.mark.parametrize("metric", ["volume_growth", "share_trend_pp",
                                    "weighted_share_trend"])
def test_a_change_metric_accepts_its_comparison(metric):
    assert reasons(plan(metric=metric, comparison=PRIOR)) == []


def test_the_refusal_names_the_metrics_that_do_compare():
    """A refusal without a way forward is a dead end."""
    found = " ".join(reasons(plan(comparison=PRIOR)))
    assert "volume_growth" in found and "share_trend_pp" in found


@pytest.mark.parametrize("grain", ["period_mo", "period_qtr", "period_wk"])
def test_a_change_metric_by_period_is_refused_by_name(grain):
    """Each side of the comparison is labelled with a different period, so
    the join matches nothing and every row comes back null."""
    found = reasons(plan(metric="volume_growth", comparison=PRIOR,
                         dimensions=[grain]))
    assert found and "cannot also be a two-window comparison" in found[0]


# ---------------------------------------------------------------------------
# Structural counts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grain", ["territory", "region", "state", "account",
                                   "parent", "facility", "gpo", "archetype",
                                   "is_340b", "org_status"])
def test_a_structural_count_accepts_every_hierarchy_grain(grain):
    """Including geography -- the grains that used to fail in SQL."""
    assert reasons(plan(metric="facility_count_all", dimensions=[grain])) == []


@pytest.mark.parametrize("grain", ["product", "ndc", "strength", "form",
                                   "market_category", "market_subcategory",
                                   "specialty", "classification", "data_source",
                                   "period_mo", "period_qtr", "period_wk"])
def test_a_structural_count_refuses_sales_side_grains(grain):
    """The organization hierarchy has no product and no period."""
    found = reasons(plan(metric="facility_count_all", dimensions=[grain]))
    assert found and grain in found[0]


def test_a_structural_count_refusal_points_at_the_sales_based_count():
    found = reasons(plan(metric="facility_count_all", dimensions=["product"]))
    assert "with-sales count" in found[0]


@pytest.mark.parametrize("field,value", [
    ("product_names", ["ZENOVAX"]), ("ndcs", ["12345-678-90"]),
    ("market_categories", ["Oncology"]), ("specialties", ["Oncology"]),
    ("classifications", ["generic"]),
])
def test_a_structural_count_refuses_product_filters(field, value):
    found = reasons(plan(metric="facility_count_all", filters={field: value}))
    assert found and field in found[0]


@pytest.mark.parametrize("field,value", [
    ("territories", ["Anywhere"]), ("regions", ["West"]), ("states", ["TX"]),
    ("gpo_names", ["Vizient"]), ("account_ids", ["GP0001"]),
])
def test_a_structural_count_accepts_hierarchy_filters(field, value):
    assert reasons(plan(metric="facility_count_all", filters={field: value})) == []


def test_several_problems_are_all_reported_at_once():
    """One round trip, not one rejection per rephrase."""
    found = reasons(plan(metric="facility_count_all", dimensions=["period_mo"],
                         filters={"product_names": ["ZENOVAX"]},
                         comparison=PRIOR))
    assert len(found) == 3


# ---------------------------------------------------------------------------
# Reachability, the rule underneath
# ---------------------------------------------------------------------------

def test_every_grain_is_reachable_from_sales():
    """The ordinary base can compute every grain the plan can name."""
    assert all(reachable(d, "sales") for d in Dimension)


def test_the_organization_base_never_reaches_the_product_side():
    assert "p" not in REACHABLE_FROM["organizations"]
    assert "c" not in REACHABLE_FROM["organizations"]
    assert not reachable(Dimension.product, "organizations")
    assert not reachable(Dimension.period_mo, "organizations")
    assert reachable(Dimension.territory, "organizations")
