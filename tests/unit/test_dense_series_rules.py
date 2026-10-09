"""When a series includes its empty periods, and what the validator admits.

The calendar-backed series needed three things the SQL validator refused --
the calendar table, a conditional, and row numbering. Each was admitted on
its own merits; these tests pin that the admission did not widen anything
else: set-returning functions stay out, and so do the application schemas.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import check_compatibility, needs_dense_series
from app.analytics.plan import AnalyticalPlan
from app.analytics.registry import get_registry
from app.analytics.validator import SqlValidationError, validate

R3M = {"kind": "named", "named": "r3m"}
DATES = {"kind": "date_range", "date_from": "2026-07-01", "date_to": "2026-09-30"}


def plan(**kw):
    return AnalyticalPlan.model_validate({"metric": "paid_pack_units", "time": R3M, **kw})


@pytest.mark.parametrize("kw,dense", [
    ({"dimensions": ["period_mo"]}, True),                              # one series
    ({"dimensions": ["period_mo"], "rolling": {"periods": 3}}, True),
    ({"dimensions": ["product", "period_mo"], "rolling": {"periods": 3}}, True),
    ({"dimensions": ["product", "period_mo"]}, False),                  # cross product
    ({"dimensions": ["account"]}, False),                               # not a series
    ({"dimensions": ["period_mo"], "time": DATES}, False),              # dates
])
def test_which_plans_get_their_empty_periods(kw, dense):
    assert needs_dense_series(plan(**kw)) is dense


def test_a_rolling_average_over_calendar_dates_is_refused_by_name():
    p = plan(dimensions=["period_mo"], rolling={"periods": 3}, time=DATES)
    found = check_compatibility(p, get_registry().get("paid_pack_units"))
    assert found and "whole periods" in found[0]


def test_the_calendar_and_its_conditionals_are_admitted():
    validate("WITH a AS (SELECT period_mo AS label, "
             "row_number() OVER (ORDER BY wk_offset) AS pos FROM app_ref.calendar) "
             "SELECT CASE WHEN pos > 1 THEN label END FROM a", wac_authorized=False)


@pytest.mark.parametrize("sql,why", [
    ("SELECT * FROM generate_series(1, 1000000000)", "unbounded generator"),
    ("SELECT * FROM app_meta.dataset_manifest", "application schema"),
    ("SELECT * FROM app_conv.turns", "conversation history"),
    ("SELECT pg_sleep(10)", "arbitrary function"),
])
def test_admitting_them_widened_nothing_else(sql, why):
    with pytest.raises(SqlValidationError):
        validate(sql, wac_authorized=True)
