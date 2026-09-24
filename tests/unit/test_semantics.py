"""Semantics that must not drift. No database required."""

from __future__ import annotations

import pytest

from app.analytics.periods import PeriodError, resolve
from app.analytics.plan import AnalyticalPlan, NamedWindow, TimeWindow
from app.analytics.registry import get_registry
from app.analytics.render import format_value
from app.data.classification import classify

ANCHOR = {
    "min_mo": 0, "max_mo": 36, "min_wk": 0, "max_wk": 155,
    "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3",
}


def offsets(named: str) -> list[int]:
    window = resolve(TimeWindow(kind="named", named=NamedWindow(named)), ANCHOR)
    return window.params[0]


# ---------------------------------------------------------------------------
# Reporting windows
# ---------------------------------------------------------------------------

def test_r3m_is_current_plus_two_prior():
    assert offsets("r3m") == [0, 1, 2]


def test_business_r6m_is_the_preceding_three_months_not_six():
    """metric_definitions.md defines R6M as the window PRECEDING R3M."""
    assert offsets("r6m_prior") == [3, 4, 5]


def test_literal_last_six_months_is_a_different_window():
    assert offsets("last_6_months") == [0, 1, 2, 3, 4, 5]
    assert offsets("last_6_months") != offsets("r6m_prior")


def test_last_quarter_is_offsets_one_to_three_not_a_calendar_quarter():
    """period_offsets.md defines it as 1-3, which at a September anchor is
    June-August, not calendar Q2."""
    assert offsets("last_quarter") == [1, 2, 3]


def test_last_month_is_the_completed_month():
    assert offsets("last_month") == [1]


def test_r30d_is_four_reporting_weeks():
    window = resolve(TimeWindow(kind="named", named=NamedWindow.r30d), ANCHOR)
    assert window.params[0] == [0, 1, 2, 3]
    assert "wk_offset" in window.sql


@pytest.mark.parametrize(
    "named,incomplete",
    [("r3m", True), ("current_month", True), ("last_6_months", True),
     ("last_month", False), ("r6m_prior", False), ("last_quarter", False)],
)
def test_windows_flag_an_incomplete_current_period(named, incomplete):
    window = resolve(TimeWindow(kind="named", named=NamedWindow(named)), ANCHOR)
    assert window.incomplete_period is incomplete


def test_available_range_is_read_from_the_anchor_not_hardcoded():
    """The documented maximum is 35 but the generator reaches 36."""
    window = resolve(TimeWindow(kind="month_offsets", month_offsets=[36]), ANCHOR)
    assert window.params[0] == [36]


def test_window_entirely_outside_the_data_is_refused():
    """Schema-valid but beyond what this dataset holds (max offset is 36)."""
    with pytest.raises(PeriodError):
        resolve(TimeWindow(kind="month_offsets", month_offsets=[200]), ANCHOR)


def test_offsets_beyond_the_schema_bound_never_reach_the_resolver():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TimeWindow(kind="month_offsets", month_offsets=[900])


def test_explicit_quarter_uses_labels_not_offsets():
    window = resolve(TimeWindow(kind="period_labels", period_labels=["2026-Q2"]), ANCHOR)
    assert "period_qtr" in window.sql


def test_mixing_label_types_in_one_window_is_refused():
    with pytest.raises(PeriodError):
        resolve(TimeWindow(kind="period_labels", period_labels=["2026-Q2", "2026-05"]), ANCHOR)


def test_date_range_warns_that_it_is_not_the_reporting_calendar():
    window = resolve(
        TimeWindow(kind="date_range", date_from="2026-08-01", date_to="2026-08-31"), ANCHOR
    )
    assert "transaction_date" in window.sql
    assert any("week-ending" in c for c in window.caveats)


def test_backwards_date_range_is_refused():
    with pytest.raises(PeriodError):
        resolve(
            TimeWindow(kind="date_range", date_from="2026-08-31", date_to="2026-08-01"), ANCHOR
        )


# ---------------------------------------------------------------------------
# Plan schema
# ---------------------------------------------------------------------------

def test_plan_rejects_unknown_keys():
    with pytest.raises(Exception):
        AnalyticalPlan.model_validate(
            {"metric": "paid_pack_units", "time": {"kind": "named", "named": "r3m"},
             "raw_sql": "SELECT 1"}
        )


def test_plan_rejects_unknown_metric():
    with pytest.raises(Exception):
        AnalyticalPlan.model_validate(
            {"metric": "everything", "time": {"kind": "named", "named": "r3m"}}
        )


def test_plan_cannot_express_a_role_or_scope():
    """Authorization inputs must be structurally absent from the plan."""
    fields = set(AnalyticalPlan.model_fields)
    assert not fields & {"role", "user_id", "scope", "principal", "can_view_wac", "sql"}


def test_change_metrics_require_a_comparison_window():
    with pytest.raises(Exception):
        AnalyticalPlan.model_validate(
            {"metric": "volume_growth", "time": {"kind": "named", "named": "r3m"}}
        )


def test_ranking_limit_is_bounded():
    with pytest.raises(Exception):
        AnalyticalPlan.model_validate(
            {"metric": "paid_pack_units", "dimensions": ["account"],
             "time": {"kind": "named", "named": "r3m"},
             "ranking": {"direction": "top", "limit": 100000}}
        )


def test_explicit_top_n_is_honoured():
    plan = AnalyticalPlan.model_validate(
        {"metric": "paid_pack_units", "dimensions": ["account"],
         "time": {"kind": "named", "named": "r3m"},
         "ranking": {"direction": "top", "limit": 5}}
    )
    assert plan.ranking.limit == 5


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_pricing_requirement_is_transitive():
    registry = get_registry()
    assert registry.requires_wac("wac_revenue")
    assert not registry.requires_wac("paid_pack_units")
    assert not registry.requires_wac("brand_market_share")


def test_market_share_numerator_and_denominator_use_different_sources():
    registry = get_registry()
    spec = registry.get("brand_market_share")
    numerator = registry.get(spec["numerator"])
    denominator = registry.get(spec["denominator"])
    assert numerator["sources"] == ["distributor"]
    assert denominator["sources"] == ["market_data"]
    # The denominator must NOT be restricted to company brand.
    assert numerator["company_only"] is True
    assert denominator["company_only"] is False
    assert spec["zero_denominator"] == "null"


def test_equivalents_use_the_conversion_factor_not_milligrams():
    registry = get_registry()
    expression = registry.component_expr("equivalents")
    assert "unit_conversion_factor" in expression
    assert "mg_equivalent" not in expression


def test_paid_demand_excludes_hub_and_market():
    spec = get_registry().get("paid_pack_units")
    assert spec["sources"] == ["distributor"]
    assert spec["company_only"] is True


def test_weighted_trend_is_declared_proposed():
    assert get_registry().get("weighted_share_trend")["proposed"] is True


def test_every_metric_anchors_to_a_supplied_document():
    for key, spec in get_registry().metrics.items():
        assert spec.get("anchor", "").startswith("docs/"), key


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_ratio_renders_as_a_percentage():
    assert format_value(0.4547717842, "ratio") == "45.48%"


def test_percentage_points_are_signed_and_distinct_from_growth():
    assert format_value(2.85, "percentage points") == "+2.85 pp"
    assert format_value(-2.86, "percentage points") == "-2.86 pp"


def test_missing_value_reads_as_unavailable_not_zero():
    assert format_value(None, "ratio") == "unavailable"
    assert format_value(None, "packs") == "unavailable"


def test_currency_formatting():
    assert format_value(36787881.3, "USD") == "$36,787,881.30"


def test_whole_pack_counts_have_no_spurious_decimals():
    assert format_value(2563.0, "packs") == "2,563 packs"
    assert format_value(1.332, "equivalents") == "1.33 equivalents"


# ---------------------------------------------------------------------------
# Derived classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,brand_flag,expected",
    [
        ("ZENOVAX", 1, "company_brand"),
        ("TAXOTERE", 0, "branded_competitor"),
        ("DOCETAXEL GENERIC", 0, "generic"),
        ("BEVACIZUMAB BIOSIMILAR", 0, "biosimilar"),
        ("KEYTRUDA", 0, "branded_competitor"),
    ],
)
def test_competitor_is_not_the_same_as_generic(name, brand_flag, expected):
    classification, _ = classify(name, brand_flag)
    assert classification == expected
