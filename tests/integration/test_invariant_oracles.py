"""Invariants that had no independent numerical oracle (qualification, 7 October 2026).

An inventory of the suite by business invariant (docs/COVERAGE_BY_INVARIANT.md)
found these computed and executed but never compared with a number worked out
another way: revenue values (only access to them was tested), the 340B share and
the market segment share (their plans were unit-tested, never their results),
and series at quarter and week grain. Each test here runs the compiled query on
the release dataset and compares it with SQL written from the documented
definition, and each requires a nonempty result, so none passes vacuously.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan
from app.analytics.validator import validate
from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]
R3M = {"kind": "named", "named": "r3m"}


@pytest.fixture(scope="module")
def anchor(pipeline):
    return pipeline.current_dataset()["reporting_anchor"]


def run(plan, anchor, scope=("global", None), wac=True):
    from app.db import analytics_transaction
    query = Compiler().compile(AnalyticalPlan.model_validate(plan), anchor=anchor)
    validate(query.sql, wac_authorized=wac)
    with analytics_transaction(scope_kind=scope[0], scope_value=scope[1], wac_authorized=wac) as cur:
        cur.execute(query.sql, query.params)
        return query, cur.fetchall()


def sql(text, params=()):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute(text, params)
        return cur.fetchall()


def close(a, b, rel=1e-9):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= rel * max(1.0, abs(float(b)))


PAID = "s.data_source = 'distributor' AND s.brand_flag = 1 AND s.mo_offset IN (0, 1, 2)"


def test_revenue_by_product_is_the_sum_of_the_transaction_amounts(anchor):
    """docs/metric_definitions.md: WAC is already a per-transaction dollar
    amount (A8), so revenue is its sum -- not volume times a price."""
    _, rows = run({"metric": "wac_revenue", "dimensions": ["product"], "time": R3M}, anchor)
    expected = {r["drug_name"]: r["v"] for r in sql(
        f"SELECT s.drug_name, sum(s.wac) AS v FROM sales s WHERE {PAID} GROUP BY 1")}
    got = {r["dim0_label"]: r["value"] for r in rows}
    assert got and set(got) == set(expected)
    assert all(close(got[k], expected[k]) for k in expected), (got, expected)
    volume_times_price = sql(f"SELECT sum(s.pack_units * s.wac) AS v FROM sales s WHERE {PAID}")[0]["v"]
    assert not close(sum(got.values()), volume_times_price), "revenue is not volume x price"


def test_the_340b_share_divides_by_all_volume_in_the_same_population(anchor):
    """Per territory: volume at 340B facilities over all volume, every other
    filter on both sides; the denominator ignores the 340B filter."""
    _, rows = run({"metric": "share_340b", "dimensions": ["territory"],
                   "filters": {"is_340b": "only"}, "time": R3M}, anchor)
    expected = {r["t"]: (r["n"], r["d"]) for r in sql(
        f"SELECT z.territory_name AS t, sum(s.pack_units) FILTER (WHERE o.is_340b = 1) AS n, "
        f"sum(s.pack_units) AS d FROM sales s JOIN organizations o ON o.org_id = s.org_id "
        f"JOIN zip_territory z ON z.zip = o.zip WHERE {PAID} GROUP BY 1")}
    got = {r["dim0_label"]: r for r in rows}
    assert got, "no territory rows"
    for territory, (num, den) in expected.items():
        if territory not in got:
            assert not num, territory          # only territories with no 340B volume may be absent
            continue
        assert close(got[territory]["value"], (num or 0) / den), territory
    assert any(0 < float(r["value"]) < 1 for r in rows), "a real proportion, not 0 or 1 everywhere"


OUR_MARKETS = "p.market_subcategory IN (SELECT market_subcategory FROM products WHERE brand_flag = 1)"
MARKET = "s.data_source = 'market_data' AND s.mo_offset IN (0, 1, 2)"
GENERIC = "p.brand_flag = 0 AND upper(p.drug_name) LIKE '%% GENERIC'"


def test_generic_segment_share_by_subcategory_uses_one_population_on_both_sides(anchor):
    """docs/market_classification.md: the numerator keeps the segment (generic,
    by the documented ' GENERIC' name rule), the denominator is the whole
    subcategory's market volume, in equivalents. With no market named the
    market is inferred from our own products (compiler: "so ZENOVAX is
    measured against Docetaxel"), and BOTH sides must use it: a subcategory
    outside our markets had a numerator and a blank share."""
    _, rows = run({"metric": "market_segment_share", "dimensions": ["market_subcategory"],
                   "filters": {"classifications": ["generic"]}, "time": R3M}, anchor)
    expected = {r["sub"]: (r["n"], r["d"]) for r in sql(
        f"SELECT p.market_subcategory AS sub, "
        f"sum(s.pack_units * p.unit_conversion_factor) FILTER (WHERE {GENERIC}) AS n, "
        f"sum(s.pack_units * p.unit_conversion_factor) AS d "
        f"FROM sales s JOIN products p ON p.ndc = s.ndc WHERE {MARKET} AND {OUR_MARKETS} GROUP BY 1")}
    got = {r["dim0_id"]: r for r in rows}
    for row in rows:
        assert row["numerator"] is None or row["denominator"] is not None, \
            f"{row['dim0_id']}: a numerator with no denominator"
        assert row["dim0_label"] == row["dim0_id"], \
            f"{row['dim0_id']}: listed without its own label"
    assert set(got) <= set(expected), f"rows outside our markets: {set(got) - set(expected)}"
    with_generics = {k: v for k, v in expected.items() if v[0]}
    assert with_generics, "our markets have generic volume"
    for sub, (num, den) in with_generics.items():
        assert close(got[sub]["value"], num / den), (sub, got[sub]["value"], num / den)


def test_the_total_generic_share_of_our_markets_divides_like_by_like(anchor):
    _, rows = run({"metric": "market_segment_share", "filters": {"classifications": ["generic"]},
                   "time": R3M}, anchor)
    [want] = sql(f"SELECT sum(s.pack_units * p.unit_conversion_factor) FILTER (WHERE {GENERIC}) AS n, "
                 f"sum(s.pack_units * p.unit_conversion_factor) AS d FROM sales s "
                 f"JOIN products p ON p.ndc = s.ndc WHERE {MARKET} AND {OUR_MARKETS}")
    assert close(rows[0]["value"], want["n"] / want["d"]), (rows[0], want)
    assert 0 < float(rows[0]["value"]) <= 1


def test_a_quarterly_series_matches_hand_written_sums(anchor):
    _, rows = run({"metric": "paid_pack_units", "dimensions": ["period_qtr"],
                   "time": {"kind": "named", "named": "all_time"}}, anchor)
    covered = {r["q"]: r["c"] for r in sql(
        "SELECT period_qtr AS q, bool_and('distributor' = ANY(sources)) AS c "
        "FROM app_ref.calendar GROUP BY 1")}
    sums = {r["q"]: r["v"] for r in sql(
        "SELECT s.period_qtr AS q, sum(s.pack_units) AS v FROM sales s "
        "WHERE s.data_source = 'distributor' AND s.brand_flag = 1 GROUP BY 1")}
    got = {r["dim0_id"]: r["value"] for r in rows}
    assert len(got) >= 4 and set(got) == set(covered)
    for quarter, is_covered in covered.items():
        want = (sums.get(quarter, 0) if is_covered else None)
        assert close(got[quarter], want), (quarter, got[quarter], want)


def test_a_weekly_series_matches_hand_written_sums(anchor):
    _, rows = run({"metric": "paid_pack_units", "dimensions": ["period_wk"],
                   "time": {"kind": "named", "named": "r30d"}}, anchor)
    sums = {r["w"]: r["v"] for r in sql(
        "SELECT s.period_wk AS w, sum(s.pack_units) AS v FROM sales s "
        "WHERE s.data_source = 'distributor' AND s.brand_flag = 1 AND s.wk_offset <= 3 GROUP BY 1")}
    got = {r["dim0_id"]: r["value"] for r in rows}
    assert len(got) == 4 and set(got) == set(sums)
    assert all(close(got[w], sums[w]) for w in sums), (got, sums)
