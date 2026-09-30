"""A snapshot is published with its calendar, and a contradictory one is not.

The calendar is rebuilt from the facts in the same transaction as the facts.
A week that maps to two months cannot place a period in a series, so it
fails the load rather than publishing an ambiguous calendar -- which is how
the coherent fixture's mislabelled rows were found.

Runs in a transaction on the DISPOSABLE authorization database and rolls
back; nothing persists.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from app.data.loader import LoadError, _populate_calendar
from app.data.manifest import LoadReport

AUTHTEST_DB = os.environ.get("PAC_AUTHTEST_DB", "pharma_analytics_authtest")


@pytest.fixture
def rollback_cursor():
    from app.config import get_settings
    settings = get_settings()
    dsn = settings.dsn("owner").replace(f"dbname={settings.db_name}", f"dbname={AUTHTEST_DB}")
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except Exception as exc:
        pytest.skip(f"authtest database unavailable: {type(exc).__name__}")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM sales")
            if cur.fetchone()["n"] == 0:
                pytest.skip("authtest database is empty")
            yield cur
    finally:
        conn.rollback()
        conn.close()


def test_the_calendar_is_rebuilt_from_the_facts(rollback_cursor):
    cur = rollback_cursor
    cur.execute("DELETE FROM app_ref.calendar")
    _populate_calendar(cur)
    cur.execute("SELECT count(*) AS n FROM app_ref.calendar")
    weeks = cur.fetchone()["n"]
    cur.execute("SELECT count(DISTINCT wk_offset) AS n FROM sales")
    assert weeks == cur.fetchone()["n"]


def test_a_week_in_two_months_fails_the_load(rollback_cursor):
    cur = rollback_cursor
    cur.execute("""
        INSERT INTO sales (org_id, ndc, drug_name, data_source, brand_flag, pack_units,
                           total_mg, wac, transaction_date, week_ending_date, state,
                           specialty, period_wk, period_mo, period_qtr, wk_offset, mo_offset)
        SELECT org_id, ndc, drug_name, data_source, brand_flag, pack_units, total_mg, wac,
               transaction_date, week_ending_date, state, specialty, period_wk,
               '1999-01', period_qtr, wk_offset, mo_offset + 1
        FROM sales LIMIT 1""")
    cur.execute("DELETE FROM app_ref.calendar")
    with pytest.raises(LoadError, match="more than one reporting month"):
        _populate_calendar(cur)


def _gap_report(cur) -> dict | None:
    cur.execute("DELETE FROM app_ref.calendar")
    report = LoadReport(dataset_id="t", load_mode="seed")
    _populate_calendar(cur, report)
    return next((w for w in report.warnings if w["code"] == "calendar_gaps"), None)


def test_a_week_missing_from_every_source_is_reported(rollback_cursor):
    """Measured as a change, not a presence: the seed data is already sparse
    (weeks 0, 1, 4, 8, ...), so "a warning exists" would pass without this
    test doing anything. Removing one interior week must raise the reported
    gap by exactly one."""
    cur = rollback_cursor

    def missing(w):
        return 0 if w is None else (w["last_week"] - w["first_week"] + 1 - w["weeks_present"])

    before = missing(_gap_report(cur))
    cur.execute("SELECT min(wk_offset) AS lo, max(wk_offset) AS hi FROM sales")
    bounds = cur.fetchone()
    cur.execute("SELECT min(wk_offset) AS w FROM sales WHERE wk_offset > %s", (bounds["lo"],))
    interior = cur.fetchone()["w"]
    assert bounds["lo"] < interior < bounds["hi"], "need a week strictly inside the range"

    cur.execute("DELETE FROM sales WHERE wk_offset = %s", (interior,))
    after = _gap_report(cur)

    assert after is not None
    assert missing(after) == before + 1
