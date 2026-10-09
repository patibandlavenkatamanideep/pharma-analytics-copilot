"""An absent period is zero only where the source covered it.

The coherent fixture has distributor rows in July and August but no market
data there. So for paid volume, July and August are measured; for market
share, whose denominator is market data, they are not -- and a share with
nothing to divide by is undefined, not zero.

Runs against the fixture database, like test_coherent_fixture.py.
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
R3M = {"kind": "named", "named": "r3m"}


@pytest.fixture(scope="module")
def fixture_env():
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = FIXTURE_DB
    get_settings.cache_clear()
    close_pools()
    clear_caches()
    try:
        with owner_transaction() as cur:
            cur.execute("SELECT count(*) AS n FROM app_ref.calendar")
            if cur.fetchone()["n"] == 0:
                pytest.skip("fixture calendar is empty; run scripts/build_fixture_db.py")
        yield
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


def series(plan_dict):
    from app.db import analytics_transaction
    q = Compiler().compile(AnalyticalPlan.model_validate(plan_dict), anchor=ANCHOR)
    validate(q.sql, wac_authorized=True)
    with analytics_transaction(scope_kind="global", scope_value=None,
                               wac_authorized=True) as cur:
        cur.execute(q.sql, q.params)
        return {r["dim0_id"]: r["value"] for r in cur.fetchall()}


def test_the_fixture_calendar_records_what_each_month_covered(fixture_env):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT period_mo, sources FROM app_ref.calendar")
        sources = {r["period_mo"]: set(r["sources"]) for r in cur.fetchall()}
    assert "market_data" not in sources["2026-08"]
    assert "market_data" in sources["2026-09"]


def test_paid_volume_in_a_covered_month_is_measured(fixture_env):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""SELECT period_mo, sum(pack_units) AS v FROM sales
                       WHERE data_source = 'distributor' AND brand_flag = 1
                         AND mo_offset IN (0, 1, 2) GROUP BY 1""")
        want = {r["period_mo"]: float(r["v"]) for r in cur.fetchall()}
    got = series({"metric": "paid_pack_units", "dimensions": ["period_mo"], "time": R3M})
    assert {k: float(v) for k, v in got.items()} == want


def test_a_share_in_a_month_without_market_data_is_unknown_not_zero(fixture_env):
    got = series({"metric": "brand_market_share", "dimensions": ["period_mo"], "time": R3M})
    assert set(got) == {"2026-07", "2026-08", "2026-09"}, "every month is listed"
    assert got["2026-08"] is None and got["2026-07"] is None
    assert got["2026-09"] is not None
