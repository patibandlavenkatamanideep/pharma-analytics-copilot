"""Time series on the reporting calendar, against hand-computed values.

Review finding 5c. A facility bought PAXELIUM in June (3 packs) and
September (2), nothing in July or August. The September "3-month rolling
average" read 2.5 -- June averaged with September, a window that does not
contain June -- and July and August were absent from the series altogether.
Separately, the first points of every rolling series averaged fewer periods
than asked, even when the earlier months were in the data.

Every expected value here comes from SQL written in this file, with its own
list of months taken from ``sales`` -- not from ``app_ref.calendar`` and not
from the compiler under test. Queries run as the runtime role under row-level
security, the way the application runs them.
"""

from __future__ import annotations

from statistics import mean

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan
from app.analytics.validator import validate
from tests.conftest import needs_full

pytestmark = needs_full


def run(anchor, plan_dict, *, scope_kind="global", scope_value=None):
    from app.db import analytics_transaction
    plan = AnalyticalPlan.model_validate(plan_dict)
    q = Compiler(max_rows=5000).compile(plan, anchor=anchor)
    validate(q.sql, wac_authorized=False)
    with analytics_transaction(scope_kind=scope_kind, scope_value=scope_value,
                               wac_authorized=False) as cur:
        cur.execute(q.sql, q.params)
        return cur.fetchall(), q


def monthly_truth(where: str, params=()) -> dict[int, float]:
    """offset -> paid packs, zero for every month the data has and the
    population did not buy in. Independent of the compiler and the calendar."""
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT DISTINCT mo_offset FROM sales")
        months = {r["mo_offset"]: 0.0 for r in cur.fetchall()}
        cur.execute(f"""
            SELECT s.mo_offset, sum(s.pack_units) AS v
            FROM sales s JOIN products p ON p.ndc = s.ndc
            WHERE s.data_source = 'distributor' AND s.brand_flag = 1 AND {where}
            GROUP BY 1""", params)
        for r in cur.fetchall():
            months[r["mo_offset"]] = float(r["v"])
    return months


def label_of(offset: int) -> str:
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT DISTINCT period_mo FROM sales WHERE mo_offset = %s", (offset,))
        return cur.fetchone()["period_mo"]


def rolling(truth: dict[int, float], offset: int, n: int) -> float | None:
    window = [offset + k for k in range(n)]          # this month and n-1 older
    if any(o not in truth for o in window):
        return None                                  # history runs out
    return mean(truth[o] for o in window)


SPARSE = {"facility_ids": ["FA0017"], "product_names": ["PAXELIUM"]}
SPARSE_SQL = ("s.org_id = %s AND upper(p.drug_name) = %s", ("FA0017", "PAXELIUM"))
LAST6 = {"kind": "named", "named": "last_6_months"}


def as_map(rows, key="value"):
    return {r["dim0_id"]: (None if r[key] is None else float(r[key])) for r in rows}


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------

def test_the_reported_sparse_series_averages_the_right_months(anchor):
    truth = monthly_truth(*SPARSE_SQL)
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "filters": SPARSE, "time": LAST6, "rolling": {"periods": 3}})
    got = as_map(rows)
    want = {label_of(o): rolling(truth, o, 3) for o in range(6)}
    assert got.keys() == want.keys()
    for label in want:
        assert got[label] == pytest.approx(want[label]), label
    # Spelled out, because this is the number the review reported as 2.5.
    assert got[label_of(0)] == pytest.approx((0 + 0 + 2) / 3)


def test_the_months_with_no_purchases_are_in_the_series(anchor):
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "filters": SPARSE, "time": LAST6})
    got = as_map(rows)
    assert len(got) == 6, "every month of the window is listed"
    assert got[label_of(1)] == 0 and got[label_of(2)] == 0


def test_the_first_point_averages_a_full_window_using_earlier_months(anchor):
    """The window starts at offset 5; its 3-month average needs 6 and 7,
    which are in the data. They used to be filtered out and the first point
    averaged itself alone."""
    truth = monthly_truth("upper(p.drug_name) = %s", ("ZENOVAX",))
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "filters": {"product_names": ["ZENOVAX"]}, "time": LAST6,
                           "rolling": {"periods": 3}})
    got = as_map(rows)
    first = label_of(5)
    assert got[first] == pytest.approx(mean([truth[5], truth[6], truth[7]]))
    assert got[first] != pytest.approx(truth[5]), "averaged the first month alone"


def test_the_unaveraged_point_is_carried_beside_the_average(anchor):
    truth = monthly_truth(*SPARSE_SQL)
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "filters": SPARSE, "time": LAST6, "rolling": {"periods": 3}})
    points = as_map(rows, "point_value")
    assert points == {label_of(o): truth[o] for o in range(6)}


# ---------------------------------------------------------------------------
# Dense, partial, empty
# ---------------------------------------------------------------------------

def test_a_dense_series_matches_the_hand_computed_average(anchor):
    truth = monthly_truth("upper(p.drug_name) = %s", ("ZENOVAX",))
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "filters": {"product_names": ["ZENOVAX"]}, "time": LAST6,
                           "rolling": {"periods": 3}})
    got = as_map(rows)
    for o in range(6):
        assert got[label_of(o)] == pytest.approx(rolling(truth, o, 3))


def test_where_history_runs_out_the_average_is_blank_not_shorter(anchor):
    """The data starts at the oldest offset. A 3-month average there has only
    one month to average and is left blank rather than reported as if it were
    three months."""
    oldest = int(anchor["max_mo"])
    offsets = [oldest - 2, oldest - 1, oldest]
    truth = monthly_truth("upper(p.drug_name) = %s", ("ZENOVAX",))
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "filters": {"product_names": ["ZENOVAX"]},
                           "time": {"kind": "month_offsets", "month_offsets": offsets},
                           "rolling": {"periods": 3}})
    got = as_map(rows)
    assert got[label_of(oldest)] is None
    assert got[label_of(oldest - 1)] is None
    assert got[label_of(oldest - 2)] == pytest.approx(rolling(truth, oldest - 2, 3))


def test_an_empty_population_is_a_series_of_known_zeros(anchor):
    """Nothing bought is a measurement -- every month covered by the source,
    zero in each -- not an absence of data."""
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "filters": {"facility_ids": ["NO-SUCH-FACILITY"]},
                           "time": LAST6, "rolling": {"periods": 3}})
    got = as_map(rows)
    assert len(got) == 6 and set(got.values()) == {0.0}


def test_each_group_gets_its_own_complete_series(anchor):
    rows, _ = run(anchor, {"metric": "paid_pack_units",
                           "dimensions": ["product", "period_mo"],
                           "filters": {"product_names": ["ZENOVAX", "PAXELIUM"],
                                       "facility_ids": ["FA0017"]},
                           "time": LAST6, "rolling": {"periods": 3}})
    by_product: dict[str, int] = {}
    for r in rows:
        by_product[r["dim0_id"]] = by_product.get(r["dim0_id"], 0) + 1
    # PAXELIUM was bought; ZENOVAX may not have been. Either way, a product
    # that appears gets all six months, not only the months it had rows.
    assert all(n == 6 for n in by_product.values()), by_product


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_a_scoped_role_reads_the_calendar_and_gets_a_complete_series(anchor):
    """The calendar is reference data, granted to the runtime roles; the
    facts under it are still row-level secured. A RAM's series has every
    month even where their territory bought nothing."""
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT territory_name FROM zip_territory ORDER BY 1 LIMIT 1")
        territory = cur.fetchone()["territory_name"]
    rows, _ = run(anchor, {"metric": "paid_pack_units", "dimensions": ["period_mo"],
                           "time": LAST6, "rolling": {"periods": 3}},
                  scope_kind="territory", scope_value=territory)
    assert len(rows) == 6
    assert all(r["value"] is not None for r in rows)
