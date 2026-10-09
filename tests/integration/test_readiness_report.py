"""The readiness report on the release dataset (the supplied data).

Read-only. Each status is checked against what the supplied data is known to
be: a calendar ingestion can extend, every product classified by the source
or the supplied mapping, a market source holding no company rows.
"""

from __future__ import annotations

import pytest

from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]


@pytest.fixture(scope="module")
def report():
    from app.data.readiness import build
    from app.db import owner_transaction

    with owner_transaction() as cur:
        built = build(cur)
        cur.execute("SELECT count(*) AS n FROM app_ingest.batches")
        built["_batches"] = cur.fetchone()["n"]
    return built


def test_the_supplied_calendar_can_be_extended(report):
    periods = report["sections"]["periods"]
    assert periods["status"] == "ready", periods
    assert periods["evidence"]["calendar"] == {
        "extendable": True, "week_ending_weekday": 5, "month_rules": ["week_ending_month"]}


def test_every_supplied_product_has_a_class(report):
    classes = report["sections"]["classifications"]
    assert classes["status"] == "ready", classes
    assert classes["evidence"]["classification"]["unknown_products"] == 0


def test_a_competitor_only_market_source_needs_attention(report):
    totals = report["sections"]["totals_and_overlap"]
    assert totals["evidence"]["market_data_company_rows"] == 0
    assert totals["status"] == "attention"


def test_unknown_is_never_reported_as_ready(report):
    assert report["real_data"] is None, "the caller did not say; the report does not guess"
    cadence = report["sections"]["cadence"]["status"]
    assert cadence == ("not_measured" if report["_batches"] == 0 else "ready")
