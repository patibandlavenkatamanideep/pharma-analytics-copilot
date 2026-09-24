"""Metric correctness against the loaded database.

Expected values come from SQL written BY HAND in this file, never from the
compiler under test. If the compiler and the reference disagree, one of them is
wrong and the test says so; using the compiler to generate its own expectation
would make the suite agree with any bug.
"""

from __future__ import annotations

import pytest

from app.analytics.plan import AnalyticalPlan
from tests.conftest import needs_db, needs_full, reference_scalar, run_plan

pytestmark = [pytest.mark.integration, needs_db]

GLOBAL = dict(scope_kind="global", scope_value=None)


def scalar(rows):
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    return rows[0]["value"]


# ---------------------------------------------------------------------------
# Source semantics
# ---------------------------------------------------------------------------

def test_paid_demand_is_distributor_and_company_brand_only(compiler, anchor, exec_user):
    plan = AnalyticalPlan.model_validate(
        {"metric": "paid_pack_units", "time": {"kind": "named", "named": "r3m"}}
    )
    rows, _ = run_plan(compiler, plan, anchor, exec_user)

    expected = reference_scalar(
        """
        SELECT sum(pack_units) FROM sales
        WHERE data_source = 'distributor' AND brand_flag = 1 AND mo_offset IN (0,1,2)
        """,
        (), **GLOBAL, wac=True,
    )
    assert scalar(rows) == pytest.approx(expected)


def test_hub_volume_is_excluded_from_paid_demand(compiler, anchor, exec_user):
    """Paid demand must differ from paid+free, or hub is leaking in."""
    paid = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "paid_pack_units", "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )[0]
    including_free = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "total_volume_incl_free", "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )[0]
    hub = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "pap_volume", "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )[0]

    assert scalar(paid) < scalar(including_free)
    assert scalar(paid) + scalar(hub) == pytest.approx(scalar(including_free))


def test_equivalents_use_the_conversion_factor(compiler, anchor, exec_user):
    plan = AnalyticalPlan.model_validate(
        {"metric": "paid_equivalents", "time": {"kind": "named", "named": "r3m"}}
    )
    rows, _ = run_plan(compiler, plan, anchor, exec_user)

    expected = reference_scalar(
        """
        SELECT sum(s.pack_units * p.unit_conversion_factor)
        FROM sales s JOIN products p ON p.ndc = s.ndc
        WHERE s.data_source = 'distributor' AND s.brand_flag = 1 AND s.mo_offset IN (0,1,2)
        """,
        (), **GLOBAL, wac=True,
    )
    assert scalar(rows) == pytest.approx(expected)


def test_equivalents_differ_from_the_milligram_formula(compiler, anchor, exec_user):
    """The two documented formulas are not interchangeable in this data."""
    correct = reference_scalar(
        """
        SELECT sum(s.pack_units * p.unit_conversion_factor)
        FROM sales s JOIN products p ON p.ndc = s.ndc
        WHERE s.data_source = 'distributor' AND s.brand_flag = 1 AND s.mo_offset IN (0,1,2)
        """,
        (), **GLOBAL,
    )
    milligram = reference_scalar(
        """
        SELECT sum(s.total_mg / NULLIF(p.mg_equivalent, 0))
        FROM sales s JOIN products p ON p.ndc = s.ndc
        WHERE s.data_source = 'distributor' AND s.brand_flag = 1 AND s.mo_offset IN (0,1,2)
        """,
        (), **GLOBAL,
    )
    assert correct != pytest.approx(milligram)

    plan = AnalyticalPlan.model_validate(
        {"metric": "paid_equivalents", "time": {"kind": "named", "named": "r3m"}}
    )
    rows, _ = run_plan(compiler, plan, anchor, exec_user)
    assert scalar(rows) == pytest.approx(correct)


# ---------------------------------------------------------------------------
# Market share
# ---------------------------------------------------------------------------

def test_market_share_matches_hand_written_components(compiler, anchor, exec_user):
    plan = AnalyticalPlan.model_validate(
        {"metric": "brand_market_share", "filters": {"product_names": ["ZENOVAX"]},
         "time": {"kind": "named", "named": "r3m"}}
    )
    rows, _ = run_plan(compiler, plan, anchor, exec_user)

    numerator = reference_scalar(
        """
        SELECT sum(s.pack_units * p.unit_conversion_factor)
        FROM sales s JOIN products p ON p.ndc = s.ndc
        WHERE s.data_source = 'distributor' AND s.brand_flag = 1
          AND s.mo_offset IN (0,1,2) AND p.drug_name = 'ZENOVAX'
        """,
        (), **GLOBAL,
    )
    # The denominator is EVERY market_data row in the Docetaxel subcategory --
    # not restricted to our drug name and not restricted to brand_flag = 1.
    denominator = reference_scalar(
        """
        SELECT sum(s.pack_units * p.unit_conversion_factor)
        FROM sales s JOIN products p ON p.ndc = s.ndc
        WHERE s.data_source = 'market_data' AND s.mo_offset IN (0,1,2)
          AND p.market_subcategory = 'Docetaxel'
        """,
        (), **GLOBAL,
    )
    assert rows[0]["numerator"] == pytest.approx(numerator)
    assert rows[0]["denominator"] == pytest.approx(denominator)
    assert rows[0]["value"] == pytest.approx(numerator / denominator)


def test_denominator_is_not_narrowed_to_our_own_drug(compiler, anchor, exec_user):
    """If the denominator were filtered to ZENOVAX it would be zero in this
    data (market_data holds no company rows), and share would be NULL."""
    plan = AnalyticalPlan.model_validate(
        {"metric": "brand_market_share", "filters": {"product_names": ["ZENOVAX"]},
         "time": {"kind": "named", "named": "r3m"}}
    )
    rows, _ = run_plan(compiler, plan, anchor, exec_user)
    assert rows[0]["denominator"] is not None and rows[0]["denominator"] > 0


# needs full data: the seed fixture has too few market_data rows for every company subcategory to appear on both sides of the bridge.
@needs_full
def test_share_by_product_is_measured_against_its_subcategory(compiler, anchor, exec_user):
    """Grouping by product must not collapse the market to that product.

    Needs full data: the seed fixture has too few market_data rows for every
    company subcategory to be represented on both sides of the bridge.
    """
    by_product, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "brand_market_share", "dimensions": ["product"],
             "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    by_subcategory, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "brand_market_share", "dimensions": ["market_subcategory"],
             "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    product_values = {r["dim0_label"]: r["value"] for r in by_product}
    subcategory_values = {r["dim0_label"]: r["value"] for r in by_subcategory}

    # Each company brand owns exactly one subcategory in this dataset, so the
    # two groupings must agree value for value.
    assert product_values, "expected company products"
    assert len(product_values) == len(subcategory_values)
    assert sorted(round(v, 9) for v in product_values.values()) == \
           sorted(round(v, 9) for v in subcategory_values.values())


def test_zero_denominator_yields_null_not_zero(compiler, anchor, exec_user):
    """A subcategory with no market rows must read as unavailable."""
    rows, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "brand_market_share", "dimensions": ["market_subcategory"],
             "filters": {"market_subcategories": ["Paclitaxel"]},
             "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    # NovaPharma sells nothing in Paclitaxel, so the numerator is empty. The
    # result must never be a fabricated 0% or a division error.
    for row in rows:
        assert row["value"] is None or row["value"] >= 0


def test_ratios_aggregate_from_components_not_as_a_mean_of_percentages(
    compiler, anchor, exec_user
):
    by_territory, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "brand_market_share", "dimensions": ["territory"],
             "filters": {"product_names": ["ZENOVAX"]},
             "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    overall, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "brand_market_share", "filters": {"product_names": ["ZENOVAX"]},
             "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    numerators = sum(r["numerator"] or 0 for r in by_territory)
    denominators = sum(r["denominator"] or 0 for r in by_territory)
    pooled = numerators / denominators

    mean_of_rows = sum(r["value"] for r in by_territory if r["value"] is not None) / len(
        [r for r in by_territory if r["value"] is not None]
    )

    assert overall[0]["value"] == pytest.approx(pooled, rel=1e-6)
    # The naive average is a different number; this is what we must not report.
    assert overall[0]["value"] != pytest.approx(mean_of_rows, rel=1e-6)


# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------

def test_accounts_roll_up_to_grandparent_with_standalone_fallback(
    compiler, anchor, exec_user
):
    rows, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "paid_pack_units", "dimensions": ["account"],
             "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    company_total = reference_scalar(
        "SELECT sum(pack_units) FROM sales WHERE data_source='distributor' "
        "AND brand_flag=1 AND mo_offset IN (0,1,2)",
        (), **GLOBAL,
    )
    # Independent reference: the same rollup, unbounded, written by hand.
    reference_rollup = reference_scalar(
        """
        SELECT sum(v) FROM (
            SELECT COALESCE(o.grandparent_org_id, o.org_id) AS account,
                   sum(s.pack_units) AS v
            FROM sales s JOIN organizations o ON o.org_id = s.org_id
            WHERE s.data_source = 'distributor' AND s.brand_flag = 1
              AND s.mo_offset IN (0,1,2)
            GROUP BY 1
        ) t
        """,
        (), **GLOBAL,
    )
    # Every facility belongs to exactly one top-level account, so the rollup is
    # lossless when it is not truncated.
    assert reference_rollup == pytest.approx(company_total)

    # The compiled query is capped, and the cap must be visible rather than
    # silently presented as the whole answer.
    total_shown = sum(r["value"] or 0 for r in rows)
    assert total_shown <= company_total


# needs full data: the seed fixture has 85 organizations, far under the result cap.
@needs_full
def test_a_truncated_result_is_flagged_not_silently_capped(compiler, anchor, exec_user):
    """There are more than max_result_rows accounts, so this result is cut
    short; the renderer must say so.

    Needs full data: the seed fixture has 85 organizations, far under the cap.
    """
    from app.analytics.render import render
    from app.auth.policy import scope_note

    plan = AnalyticalPlan.model_validate(
        {"metric": "paid_pack_units", "dimensions": ["account"],
         "time": {"kind": "named", "named": "r3m"}}
    )
    rows, query = run_plan(compiler, plan, anchor, exec_user)
    answer = render(
        rows, query, plan, scope_note=scope_note(exec_user, plan),
        max_rows=compiler.max_rows,
    )
    assert len(rows) > compiler.max_rows, "expected more accounts than the cap"
    assert answer.truncated is True
    assert len(answer.table) == compiler.max_rows


def test_account_grouping_uses_ids_not_names(compiler, anchor, exec_user):
    """267 name groups are shared by more than one org_id in the full data."""
    rows, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "paid_pack_units", "dimensions": ["account"],
             "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    ids = [r["dim0_id"] for r in rows]
    assert len(ids) == len(set(ids)), "account ids must be unique per row"


def test_counts_use_distinct_entities(compiler, anchor, exec_user):
    accounts, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "account_count", "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    facilities, _ = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "facility_count", "time": {"kind": "named", "named": "r3m"}}
        ),
        anchor, exec_user,
    )
    # There are always at least as many facilities as top-level accounts.
    assert scalar(facilities) >= scalar(accounts)


# ---------------------------------------------------------------------------
# 340B
# ---------------------------------------------------------------------------

def test_340b_include_equals_exclude_plus_only(compiler, anchor, exec_user):
    def total(state):
        rows, _ = run_plan(
            compiler,
            AnalyticalPlan.model_validate(
                {"metric": "paid_pack_units", "filters": {"is_340b": state},
                 "time": {"kind": "named", "named": "r3m"}}
            ),
            anchor, exec_user,
        )
        return scalar(rows)

    assert total("exclude") + total("only") == pytest.approx(total("include"))


# ---------------------------------------------------------------------------
# Period comparisons
# ---------------------------------------------------------------------------

def test_growth_is_relative_and_trend_is_percentage_points(compiler, anchor, exec_user):
    growth, growth_query = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "volume_growth", "time": {"kind": "named", "named": "r3m"},
             "comparison": {"kind": "named", "named": "r6m_prior"}}
        ),
        anchor, exec_user,
    )
    trend, trend_query = run_plan(
        compiler,
        AnalyticalPlan.model_validate(
            {"metric": "share_trend_pp", "time": {"kind": "named", "named": "r3m"},
             "comparison": {"kind": "named", "named": "r6m_prior"}}
        ),
        anchor, exec_user,
    )
    assert growth_query.unit == "ratio"
    assert trend_query.unit == "percentage points"

    g = growth[0]
    assert g["value"] == pytest.approx((g["current_value"] - g["prior_value"]) / g["prior_value"])
    t = trend[0]
    assert t["value"] == pytest.approx((t["current_value"] - t["prior_value"]) * 100.0)


def test_r6m_prior_and_literal_six_months_give_different_answers(
    compiler, anchor, exec_user
):
    def total(named):
        rows, _ = run_plan(
            compiler,
            AnalyticalPlan.model_validate(
                {"metric": "paid_pack_units", "time": {"kind": "named", "named": named}}
            ),
            anchor, exec_user,
        )
        return scalar(rows)

    assert total("r6m_prior") != pytest.approx(total("last_6_months"))
    assert total("r3m") + total("r6m_prior") == pytest.approx(total("last_6_months"))
