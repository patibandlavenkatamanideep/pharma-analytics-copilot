"""Metric behaviour on a hand-calculated coherent market.

Runs against a SEPARATE fixture database, never the application database, so
baseline evaluation and fixture evaluation can never be confused with each
other. Every expected number here was worked out by hand in the fixture file's
header comment, not produced by this system.

Build the fixture database once with:

    scripts/build_fixture_db.py
"""

from __future__ import annotations

import os
import pathlib

import pytest

from app.analytics.plan import AnalyticalPlan

FIXTURE_DB = os.environ.get("PAC_FIXTURE_DB", "pharma_analytics_fixture")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def fixture_env():
    """Point the settings and pools at the fixture database for this module."""
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = FIXTURE_DB
    get_settings.cache_clear()
    close_pools()
    clear_caches()

    try:
        from app.db import owner_transaction

        with owner_transaction() as cur:
            cur.execute("SELECT count(*) AS n FROM sales")
            if cur.fetchone()["n"] == 0:
                pytest.skip("fixture database is empty; run scripts/build_fixture_db.py")
        yield
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        pytest.skip(f"fixture database unavailable: {type(exc).__name__}: {exc}")
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


ANCHOR = {
    "min_mo": 0, "max_mo": 3, "min_wk": 0, "max_wk": 13,
    "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3",
}


def run(plan_dict, *, scope_kind="global", scope_value=None, wac=True):
    from app.analytics.compiler import Compiler
    from app.analytics.validator import validate
    from app.db import analytics_transaction

    plan = AnalyticalPlan.model_validate(plan_dict)
    query = Compiler().compile(plan, anchor=ANCHOR)
    validate(query.sql, wac_authorized=wac)
    with analytics_transaction(
        scope_kind=scope_kind, scope_value=scope_value, wac_authorized=wac
    ) as cur:
        cur.execute(query.sql, query.params)
        return cur.fetchall()


R3M = {"kind": "named", "named": "r3m"}

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The documented formula, on data that can actually satisfy it
# ---------------------------------------------------------------------------

def test_market_share_is_exactly_forty_percent(fixture_env):
    """80 company equivalents / 200 total market equivalents = 0.40."""
    rows = run({"metric": "brand_market_share",
                "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    assert rows[0]["numerator"] == pytest.approx(80.0)
    assert rows[0]["denominator"] == pytest.approx(200.0)
    assert rows[0]["value"] == pytest.approx(0.40)


def test_denominator_includes_our_own_brand_when_the_market_reports_it(fixture_env):
    """30 of the 200 market equivalents are ZENOVAX; excluding them would give
    80/170, which is the mistake this fixture exists to catch."""
    rows = run({"metric": "brand_market_share",
                "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    assert rows[0]["denominator"] == pytest.approx(200.0)
    assert rows[0]["value"] != pytest.approx(80 / 170)


def test_conversion_factors_are_applied_per_strength(fixture_env):
    """40 packs of the 20MG vial at 0.25 is 10 equivalents, not 40."""
    equivalents = run({"metric": "paid_equivalents",
                       "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    packs = run({"metric": "paid_pack_units",
                 "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    assert equivalents[0]["value"] == pytest.approx(80.0)
    assert packs[0]["value"] == pytest.approx(110.0)


# ---------------------------------------------------------------------------
# Metamorphic properties
# ---------------------------------------------------------------------------

def test_free_drug_does_not_change_paid_demand_or_share(fixture_env):
    """The fixture contains 20 packs of hub volume. Paid demand is 110 packs
    and share is 40% with it present -- adding free drug must move neither."""
    paid = run({"metric": "paid_pack_units",
                "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    share = run({"metric": "brand_market_share",
                 "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    total = run({"metric": "total_volume_incl_free",
                 "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    pap = run({"metric": "pap_volume",
               "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})

    assert paid[0]["value"] == pytest.approx(110.0)
    assert share[0]["value"] == pytest.approx(0.40)
    assert pap[0]["value"] == pytest.approx(20.0)
    assert total[0]["value"] == pytest.approx(130.0)


def test_zero_denominator_is_null_not_zero_and_not_an_error(fixture_env):
    """Paclitaxel has company volume but no market volume."""
    rows = run({"metric": "brand_market_share",
                "filters": {"market_subcategories": ["Paclitaxel"]}, "time": R3M})
    assert rows[0]["value"] is None


def test_market_without_company_volume_is_not_reported_as_an_error(fixture_env):
    """Carboplatin has market volume but no company volume."""
    rows = run({"metric": "brand_market_share",
                "filters": {"market_subcategories": ["Carboplatin"]}, "time": R3M})
    assert rows[0]["denominator"] == pytest.approx(25.0)
    assert rows[0]["numerator"] is None or rows[0]["numerator"] == 0


def test_duplicate_names_are_not_merged(fixture_env):
    """SOLO-1 and SOLO-2 share a name and must stay two accounts."""
    rows = run({"metric": "paid_pack_units", "dimensions": ["account"],
                "time": {"kind": "month_offsets", "month_offsets": [1]}})
    solos = [r for r in rows if r["dim0_label"] == "Independent Cancer Center"]
    assert len(solos) == 2
    assert {r["dim0_id"] for r in solos} == {"SOLO-1", "SOLO-2"}
    assert {r["value"] for r in solos} == {7.0, 9.0}


def test_standalone_facility_is_its_own_top_level_account(fixture_env):
    rows = run({"metric": "paid_pack_units", "dimensions": ["account"],
                "time": {"kind": "month_offsets", "month_offsets": [1]}})
    ids = {r["dim0_id"] for r in rows}
    assert "SOLO-1" in ids  # falls back to the facility's own id


def test_340b_exclusion_removes_only_that_facility(fixture_env):
    """ACME-3 contributes 40 packs and is the only 340B facility."""
    everything = run({"metric": "paid_pack_units",
                      "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    excluded = run({"metric": "paid_pack_units",
                    "filters": {"product_names": ["ZENOVAX"], "is_340b": "exclude"},
                    "time": R3M})
    only = run({"metric": "paid_pack_units",
                "filters": {"product_names": ["ZENOVAX"], "is_340b": "only"},
                "time": R3M})
    assert only[0]["value"] == pytest.approx(40.0)
    assert excluded[0]["value"] + only[0]["value"] == pytest.approx(everything[0]["value"])


def test_active_only_excludes_historical_inactive_volume(fixture_env):
    """OLD-1 is inactive but still has 13 packs of history at offset 2."""
    window = {"kind": "month_offsets", "month_offsets": [2]}
    everything = run({"metric": "paid_pack_units", "time": window})
    active = run({"metric": "paid_pack_units",
                  "filters": {"active_only": True}, "time": window})
    assert everything[0]["value"] == pytest.approx(13.0)
    assert active[0]["value"] is None or active[0]["value"] == 0


# ---------------------------------------------------------------------------
# Scope, at facility level, before hierarchy rollup
# ---------------------------------------------------------------------------

def test_visible_grandparent_does_not_expose_facilities_in_other_territories(
    fixture_env,
):
    """ACME-1 (New York Metro) and ACME-2 (Texas) share grandparent GP1.

    A RAM in New York Metro must see ACME-1's 50 packs and NOT ACME-2's 20,
    even though the account they roll up to is the same system.
    """
    rows = run(
        {"metric": "paid_pack_units", "dimensions": ["account"],
         "filters": {"product_names": ["ZENOVAX"]}, "time": R3M},
        scope_kind="territory", scope_value="New York Metro", wac=False,
    )
    acme = [r for r in rows if r["dim0_id"] == "GP1"]
    assert len(acme) == 1
    # 50 (ACME-1) + 10 equivalents worth of packs 40 (ACME-3, also NY) = 90 packs.
    # ACME-2's 20 packs in Texas must be absent.
    assert acme[0]["value"] == pytest.approx(90.0)


def test_exec_sees_the_whole_system_across_territories(fixture_env):
    rows = run({"metric": "paid_pack_units", "dimensions": ["account"],
                "filters": {"product_names": ["ZENOVAX"]}, "time": R3M})
    acme = [r for r in rows if r["dim0_id"] == "GP1"]
    assert acme[0]["value"] == pytest.approx(110.0)  # 50 + 20 + 40


def test_unmapped_zip_is_hidden_from_scoped_roles_but_kept_for_exec(fixture_env):
    """LOST-1 has 11 packs and a ZIP with no territory mapping."""
    exec_rows = run({"metric": "paid_pack_units", "dimensions": ["facility"],
                     "filters": {"product_names": ["NOVATAXEL"]}, "time": R3M})
    assert any(r["dim0_id"] == "LOST-1" for r in exec_rows)

    ram_rows = run(
        {"metric": "paid_pack_units", "dimensions": ["facility"],
         "filters": {"product_names": ["NOVATAXEL"]}, "time": R3M},
        scope_kind="territory", scope_value="New York Metro", wac=False,
    )
    assert not any(r["dim0_id"] == "LOST-1" for r in ram_rows)


def test_share_trend_is_in_percentage_points(fixture_env):
    """R3M share 80/200 = 40%; prior 40/160 = 25%; difference = +15 points."""
    rows = run({"metric": "share_trend_pp",
                "filters": {"product_names": ["ZENOVAX"]},
                "time": R3M,
                "comparison": {"kind": "month_offsets", "month_offsets": [3]}})
    assert rows[0]["current_value"] == pytest.approx(0.40)
    assert rows[0]["prior_value"] == pytest.approx(0.25)
    assert rows[0]["value"] == pytest.approx(15.0)
