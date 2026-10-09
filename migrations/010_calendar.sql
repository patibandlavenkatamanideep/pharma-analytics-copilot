-- The reporting calendar, one row per week, as the dataset itself defines it.
--
-- A rolling average was computed over the rows that happened to exist. A
-- facility that bought in June and September, and nothing in between, got a
-- September "3-month average" of June and September -- a window that does
-- not contain June. A time series needs every period, including the empty
-- ones, and "every period" is a property of the calendar, not of whichever
-- rows survived the question's filters and the caller's scope.
--
-- The columns deliberately mirror sales (wk_offset, mo_offset, period_*), so
-- the compiler applies the SAME resolved window predicate here as it does to
-- the facts. There is one definition of "last quarter", not two.
--
-- Weeks nest in months (week-ending convention) and months in quarters, so
-- months and quarters derive from this table by grouping. `sources` records
-- which data sources have any row in the week: an absent period is zero only
-- where the source covered it, and unknown where it did not.
CREATE TABLE IF NOT EXISTS app_ref.calendar (
    wk_offset        INTEGER PRIMARY KEY,
    period_wk        TEXT    NOT NULL UNIQUE,
    week_ending_date TEXT    NOT NULL UNIQUE,
    mo_offset        INTEGER NOT NULL,
    period_mo        TEXT    NOT NULL,
    period_qtr       TEXT    NOT NULL,
    sources          TEXT[]  NOT NULL
);

COMMENT ON TABLE app_ref.calendar IS
    'Reporting weeks with their month and quarter, rebuilt with every published snapshot. Reference data: no restricted values.';

GRANT SELECT ON app_ref.calendar TO pac_rt_exec, pac_rt_scoped;

-- Backfill from the facts already published, so an existing database gains a
-- calendar without a reload. Rebuilt, not appended: re-running this migration
-- must converge on the same table.
DELETE FROM app_ref.calendar;
INSERT INTO app_ref.calendar
    (wk_offset, period_wk, week_ending_date, mo_offset, period_mo, period_qtr, sources)
SELECT wk_offset, period_wk, week_ending_date, mo_offset, period_mo, period_qtr,
       array_agg(DISTINCT data_source ORDER BY data_source)
FROM sales
GROUP BY wk_offset, period_wk, week_ending_date, mo_offset, period_mo, period_qtr;
