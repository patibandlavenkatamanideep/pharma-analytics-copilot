"""Each period against the one before it, against an independent oracle.

The fixture calendar has four reporting months, June to September 2026, one
week each. The distributor source covered all four; market data covered June
and September only. This module adds two products of its own for the length of
the module and removes them afterwards:

  SERIESMAB (distributor, factor 0.5), packs by month:
      June 10 (ACME-1, New York) -- the first month of the data: no prior
      July none                  -- covered, so zero: a ZERO prior for August
      August 6 (ACME-1)
      September 12 (ACME-1) + 8 (ACME-2, Texas) = 20

  RETURNMAB, a defensive case the ingestion contract cannot produce (it
  refuses non-positive packs): June 4, July -2, August 3, September 3. A
  NEGATIVE prior for August.

Expected values come two ways, neither from the compiler: SQL written here
plus a lag computed in Python, and the literal numbers above. A percentage is
blank where the prior is zero or negative, a change is blank where either
month is unknown or there is no earlier month, and nothing is imputed as zero
where a source did not cover a month.

The first tests reproduced k-07's missing capability on the unmodified code
(evidence/runs/r5-k07-reproduced.json): no plan could express it.
"""

from __future__ import annotations

import os

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan
from app.analytics.validator import validate

FIXTURE_DB = os.environ.get("PAC_FIXTURE_DB", "pharma_analytics_fixture")
ANCHOR = {"min_mo": 0, "max_mo": 3, "min_wk": 0, "max_wk": 13,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}
ALL = {"kind": "named", "named": "all_time"}
MONTHS = ["2026-06", "2026-07", "2026-08", "2026-09"]
K07 = pytest.mark.xfail(strict=True, reason="k-07: per-period change is not expressible")

#: month -> (week label, week ending, quarter, wk_offset, mo_offset), from the fixture calendar
CAL = {"2026-06": ("2026-W25", "2026-06-20", "2026-Q2", 13, 3),
       "2026-07": ("2026-W29", "2026-07-18", "2026-Q3", 9, 2),
       "2026-08": ("2026-W34", "2026-08-22", "2026-Q3", 4, 1),
       "2026-09": ("2026-W38", "2026-09-19", "2026-Q3", 0, 0)}
PRODUCTS = [("33333-0101-01", "SERIESMAB", 0.5), ("33333-0201-01", "RETURNMAB", 1.0)]
ROWS = [  # (org, state, ndc, drug, month, packs)
    ("ACME-1", "NY", "33333-0101-01", "SERIESMAB", "2026-06", 10),
    ("ACME-1", "NY", "33333-0101-01", "SERIESMAB", "2026-08", 6),
    ("ACME-1", "NY", "33333-0101-01", "SERIESMAB", "2026-09", 12),
    ("ACME-2", "TX", "33333-0101-01", "SERIESMAB", "2026-09", 8),
    ("ACME-1", "NY", "33333-0201-01", "RETURNMAB", "2026-06", 4),
    ("ACME-1", "NY", "33333-0201-01", "RETURNMAB", "2026-07", -2),
    ("ACME-1", "NY", "33333-0201-01", "RETURNMAB", "2026-08", 3),
    ("ACME-1", "NY", "33333-0201-01", "RETURNMAB", "2026-09", 3),
]


@pytest.fixture(scope="module")
def series_env():
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = FIXTURE_DB
    get_settings.cache_clear()
    close_pools()
    clear_caches()

    def remove(cur):
        cur.execute("DELETE FROM sales WHERE ndc LIKE '33333-%'")
        cur.execute("DELETE FROM products WHERE ndc LIKE '33333-%'")

    try:
        with owner_transaction() as cur:
            cur.execute("SELECT count(*) AS n FROM app_ref.calendar")
            if cur.fetchone()["n"] == 0:
                pytest.skip("fixture calendar is empty; run scripts/build_fixture_db.py")
            remove(cur)
            for ndc, drug, factor in PRODUCTS:
                cur.execute(
                    "INSERT INTO products (ndc, drug_name, generic_name, strength, form, brand_flag, "
                    "specialty, market_category, market_subcategory, unit_conversion_factor, "
                    "mg_equivalent) VALUES (%s, %s, 'testumab', '10MG', 'Injectable', 1, "
                    "'Oncology', 'Series Test', 'Series Test', %s, 10.0)", (ndc, drug, factor))
            for org, state, ndc, drug, month, packs in ROWS:
                wk, ending, qtr, wk_off, mo_off = CAL[month]
                cur.execute(
                    "INSERT INTO sales (org_id, ndc, drug_name, data_source, brand_flag, pack_units, "
                    "total_mg, wac, transaction_date, week_ending_date, state, specialty, period_wk, "
                    "period_mo, period_qtr, wk_offset, mo_offset) VALUES "
                    "(%s, %s, %s, 'distributor', 1, %s, %s, %s, %s, %s, %s, 'Oncology', %s, %s, %s, %s, %s)",
                    (org, ndc, drug, packs, packs * 10, packs * 100, ending, ending, state,
                     wk, month, qtr, wk_off, mo_off))
        yield
    finally:
        with owner_transaction() as cur:
            remove(cur)
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


def run(plan_dict, *, scope=("global", None)):
    from app.db import analytics_transaction
    query = Compiler().compile(AnalyticalPlan.model_validate(plan_dict), anchor=ANCHOR)
    validate(query.sql, wac_authorized=True)
    with analytics_transaction(scope_kind=scope[0], scope_value=scope[1],
                               wac_authorized=scope[0] == "global") as cur:
        cur.execute(query.sql, query.params)
        return query, cur.fetchall()


def as_float(v):
    return None if v is None else float(v)


def oracle(where: str, params: tuple, value_sql: str = "sum(s.pack_units)", source="distributor"):
    """Monthly values by hand-written SQL, then the lag in Python."""
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT period_mo, %s = ANY(sources) AS covered FROM app_ref.calendar",
                    (source,))
        covered = {r["period_mo"]: r["covered"] for r in cur.fetchall()}
        cur.execute(f"SELECT s.period_mo, {value_sql} AS v FROM sales s "
                    f"JOIN products p ON p.ndc = s.ndc "
                    f"LEFT JOIN organizations o ON o.org_id = s.org_id "
                    f"LEFT JOIN zip_territory z ON z.zip = o.zip "
                    f"WHERE {where} GROUP BY s.period_mo", params)
        sums = {r["period_mo"]: float(r["v"]) for r in cur.fetchall()}
    out, prior = {}, None
    for month in MONTHS:
        value = sums.get(month, 0.0) if covered[month] else None
        change = value - prior if value is not None and prior is not None else None
        pct = change / prior if change is not None and prior > 0 else None
        out[month] = {"value": value, "prior": prior, "change": change, "pct": pct}
        prior = value
    return out


def got(rows, key="dim0_id"):
    return {r[key]: {"value": as_float(r["value"]), "prior": as_float(r["prior_value"]),
                     "change": as_float(r["change"]), "pct": as_float(r["change_pct"])}
            for r in rows}


def close(a, b):
    return a == b or (a is not None and b is not None and abs(a - b) < 1e-9)


def assert_same(actual, expected):
    assert set(actual) == set(expected), (sorted(actual), sorted(expected))
    for month, want in expected.items():
        for field, value in want.items():
            assert close(actual[month][field], value), (month, field, actual[month], want)


PAID = "s.data_source = 'distributor' AND s.brand_flag = 1 AND s.drug_name = %s"


@K07
def test_each_month_against_the_one_before_matches_the_oracle(series_env):
    _, rows = run({"metric": "paid_pack_units", "dimensions": ["period_mo"],
                   "filters": {"product_names": ["SERIESMAB"]}, "time": ALL,
                   "period_over_period": True})
    actual = got(rows)
    assert_same(actual, oracle(PAID, ("SERIESMAB",)))
    # The same numbers, written out: first month, zero prior, a rise.
    assert actual["2026-06"] == {"value": 10.0, "prior": None, "change": None, "pct": None}
    assert actual["2026-07"] == {"value": 0.0, "prior": 10.0, "change": -10.0, "pct": -1.0}
    assert actual["2026-08"] == {"value": 6.0, "prior": 0.0, "change": 6.0, "pct": None}
    assert actual["2026-09"]["change"] == 14.0 and close(actual["2026-09"]["pct"], 14 / 6)


@K07
def test_the_windows_first_month_is_compared_with_the_month_before_the_window(series_env):
    """r3m is July to September. July's change is against June, which is
    outside the window but in the data -- not blank, and not against zero."""
    _, rows = run({"metric": "paid_pack_units", "dimensions": ["period_mo"],
                   "filters": {"product_names": ["SERIESMAB"]},
                   "time": {"kind": "named", "named": "r3m"}, "period_over_period": True})
    actual = got(rows)
    assert sorted(actual) == ["2026-07", "2026-08", "2026-09"]
    assert actual["2026-07"]["prior"] == 10.0 and actual["2026-07"]["change"] == -10.0


@K07
def test_a_negative_prior_gives_a_change_but_no_percentage(series_env):
    _, rows = run({"metric": "paid_pack_units", "dimensions": ["period_mo"],
                   "filters": {"product_names": ["RETURNMAB"]}, "time": ALL,
                   "period_over_period": True})
    actual = got(rows)
    assert_same(actual, oracle(PAID, ("RETURNMAB",)))
    assert actual["2026-08"] == {"value": 3.0, "prior": -2.0, "change": 5.0, "pct": None}
    assert actual["2026-07"]["pct"] == -1.5          # 4 -> -2: a fall of 150%
    assert actual["2026-09"]["change"] == 0.0 and actual["2026-09"]["pct"] == 0.0


@K07
def test_explicit_units_change_in_their_own_unit(series_env):
    query, rows = run({"metric": "paid_equivalents", "dimensions": ["period_mo"],
                       "filters": {"product_names": ["SERIESMAB"]}, "time": ALL,
                       "period_over_period": True})
    assert query.unit == "equivalents"
    assert_same(got(rows), oracle(PAID, ("SERIESMAB",),
                                  value_sql="sum(s.pack_units * p.unit_conversion_factor)"))


@K07
def test_several_products_each_compare_with_their_own_prior(series_env):
    """Partitioned by product: SERIESMAB's June is not compared with
    RETURNMAB's September, and each product's first month has no prior."""
    _, rows = run({"metric": "paid_pack_units", "dimensions": ["product", "period_mo"],
                   "filters": {"product_names": ["SERIESMAB", "RETURNMAB"]}, "time": ALL,
                   "period_over_period": True})
    by_product: dict[str, list] = {}
    for r in rows:
        by_product.setdefault(r["dim0_label"], []).append(r)
    assert set(by_product) == {"SERIESMAB", "RETURNMAB"}
    for drug, product_rows in by_product.items():
        assert_same(got(product_rows, key="dim1_id"), oracle(PAID, (drug,)))


@K07
def test_a_month_a_source_did_not_cover_is_unknown_not_zero(series_env):
    """Market data covered June and September only. July and August are
    unknown, so no change involving them is computed -- September is not a
    rise from an imputed zero."""
    _, rows = run({"metric": "market_equivalents", "dimensions": ["period_mo"],
                   "filters": {"market_subcategories": ["Docetaxel"]}, "time": ALL,
                   "period_over_period": True})
    actual = got(rows)
    expected = oracle("s.data_source = 'market_data' AND p.market_subcategory = %s",
                      ("Docetaxel",), value_sql="sum(s.pack_units * p.unit_conversion_factor)",
                      source="market_data")
    assert_same(actual, expected)
    assert actual["2026-07"]["value"] is None and actual["2026-08"]["value"] is None
    assert actual["2026-09"]["value"] is not None and actual["2026-09"]["change"] is None


@K07
def test_a_scoped_user_sees_the_change_in_their_own_territory_only(series_env):
    """A RAM for New York Metro: September is 12 (ACME-1), not 20 -- the Texas
    facility's 8 packs are outside their scope, and so is any change they cause."""
    _, rows = run({"metric": "paid_pack_units", "dimensions": ["period_mo"],
                   "filters": {"product_names": ["SERIESMAB"]}, "time": ALL,
                   "period_over_period": True}, scope=("territory", "New York Metro"))
    actual = got(rows)
    assert_same(actual, oracle(PAID + " AND z.territory_name = %s", ("SERIESMAB", "New York Metro")))
    assert actual["2026-09"]["value"] == 12.0 and actual["2026-09"]["change"] == 6.0
    assert sum(1 for r in actual.values() if r["value"] is not None) == 4, "a nonempty scope"
