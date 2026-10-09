"""How ingestion labels a week it has not seen: the published calendar's own
conventions, detected and checked, or a refusal.

Pure functions over calendar rows; no database.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.data.ingest import (
    Calendar, CalendarWeek, ConventionError, detect_convention, iso_week, quarter_of,
)


def weeks_ending(last: date, n: int, month_of) -> list[CalendarWeek]:
    """n consecutive weeks ending on `last`, offsets as distances, months by
    `month_of(week_ending)`."""
    out = []
    months: dict[str, int] = {}
    for i in range(n):
        we = last - timedelta(weeks=i)
        mo = month_of(we)
        months.setdefault(mo, len(months))
        out.append(CalendarWeek(i, iso_week(we), we, months[mo], mo, quarter_of(mo)))
    return out


def ending_month(we: date) -> str:
    return we.strftime("%Y-%m")


def majority_month(we: date) -> str:
    return (we - timedelta(days=3)).strftime("%Y-%m")


def test_a_saturday_calendar_by_week_ending_month():
    # 2026-10-03 is a Saturday whose week is mostly September: the two rules
    # disagree there, so only one can be consistent with this calendar.
    cal = weeks_ending(date(2026, 10, 3), 12, ending_month)
    conv = detect_convention(cal)
    assert conv.week_ending_weekday == 5
    assert conv.month_rules == ("week_ending_month",)


def test_a_sunday_calendar_by_majority_month():
    cal = weeks_ending(date(2026, 10, 4), 12, majority_month)
    conv = detect_convention(cal)
    assert conv.week_ending_weekday == 6
    assert conv.month_rules == ("majority_month",)


@pytest.mark.parametrize("day, expected", [
    (date(2026, 9, 13), date(2026, 9, 19)),    # Sunday -> following Saturday
    (date(2026, 9, 18), date(2026, 9, 19)),    # Friday
    (date(2026, 9, 19), date(2026, 9, 19)),    # the Saturday itself
])
def test_a_day_belongs_to_the_week_ending_on_or_after_it(day, expected):
    conv = detect_convention(weeks_ending(date(2026, 9, 12), 6, ending_month))
    assert conv.week_ending(day) == expected


def _broken(mutate) -> list[CalendarWeek]:
    cal = weeks_ending(date(2026, 9, 19), 8, ending_month)
    mutate(cal)
    return cal


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.__setitem__(0, CalendarWeek(0, "2026-W38", date(2026, 9, 20), 0,
                                             "2026-09", "2026-Q3")), "weekdays"),
    (lambda c: c.__setitem__(0, CalendarWeek(0, "2026-W01", date(2026, 9, 19), 0,
                                             "2026-09", "2026-Q3")), "ISO week"),
    (lambda c: c.__setitem__(0, CalendarWeek(0, "2026-W38", date(2026, 9, 19), 0,
                                             "2026-09", "2026-Q4")), "quarter"),
    (lambda c: c.__setitem__(0, CalendarWeek(0, "2026-W38", date(2026, 9, 19), 0,
                                             "2026-11", "2026-Q4")), "no recognised rule"),
    (lambda c: c.__setitem__(1, CalendarWeek(1, c[1].period_wk, c[1].week_ending, 5,
                                             c[1].period_mo, c[1].period_qtr)),
     "more than one month offset"),
    (lambda c: c.__setitem__(1, CalendarWeek(0, c[1].period_wk, c[1].week_ending,
                                             c[1].mo_offset, c[1].period_mo,
                                             c[1].period_qtr)), "do not decrease"),
])
def test_a_calendar_with_no_consistent_convention_is_refused(mutate, message):
    with pytest.raises(ConventionError, match=message):
        detect_convention(_broken(mutate))


def test_an_empty_calendar_is_refused():
    with pytest.raises(ConventionError):
        detect_convention([])


def test_a_known_week_keeps_its_published_labels():
    cal = weeks_ending(date(2026, 9, 19), 6, ending_month)
    calendar = Calendar(cal, detect_convention(cal))
    placed = calendar.place(date(2026, 9, 9))
    assert placed.existing_week and placed.wk_offset == 1 and placed.week_ending == date(2026, 9, 12)


def test_a_new_week_after_the_anchor_gets_a_negative_provisional_offset():
    cal = weeks_ending(date(2026, 9, 19), 6, ending_month)
    calendar = Calendar(cal, detect_convention(cal))
    placed = calendar.place(date(2026, 10, 8))      # week ending Sat 10 Oct
    assert (placed.wk_offset, placed.period_wk, placed.period_mo, placed.mo_offset) == (
        -3, "2026-W41", "2026-10", -1)
    again = calendar.place(date(2026, 10, 5))
    assert again.existing_week and again.wk_offset == -3


def test_rules_that_agree_on_history_but_not_on_the_new_week_refuse_to_guess():
    # Four weeks inside September: both month rules fit them all.
    cal = weeks_ending(date(2026, 9, 26), 4, ending_month)
    conv = detect_convention(cal)
    assert set(conv.month_rules) == {"week_ending_month", "majority_month"}
    calendar = Calendar(cal, conv)
    # Sat 3 Oct: October by its ending, September by most of its days.
    assert calendar.place(date(2026, 10, 1)) == "period_convention_ambiguous"
    # Sat 10 Oct is October either way.
    assert calendar.place(date(2026, 10, 8)).period_mo == "2026-10"


def test_a_week_before_the_first_published_week_is_refused():
    cal = weeks_ending(date(2026, 9, 19), 4, ending_month)
    assert Calendar(cal, detect_convention(cal)).place(date(2026, 1, 5)) == "before_history"


def test_a_gap_week_whose_offset_would_reorder_the_calendar_is_refused():
    # Offsets that are not distances: 30 Aug is offset 2 though three weeks
    # back, 2 Aug offset 3. The week ending 23 Aug is four weeks back -- an
    # offset no other week has, but larger than the EARLIER week's 3.
    cal = [CalendarWeek(0, iso_week(date(2026, 9, 20)), date(2026, 9, 20), 0, "2026-09", "2026-Q3"),
           CalendarWeek(2, iso_week(date(2026, 8, 30)), date(2026, 8, 30), 1, "2026-08", "2026-Q3"),
           CalendarWeek(3, iso_week(date(2026, 8, 2)), date(2026, 8, 2), 2, "2026-07", "2026-Q3")]
    calendar = Calendar(cal, detect_convention(cal))
    assert calendar.place(date(2026, 8, 20)) == "calendar_conflict"


def test_a_gap_week_whose_offset_is_taken_is_refused():
    cal = [CalendarWeek(0, iso_week(date(2026, 9, 20)), date(2026, 9, 20), 0, "2026-09", "2026-Q3"),
           CalendarWeek(2, iso_week(date(2026, 8, 30)), date(2026, 8, 30), 1, "2026-08", "2026-Q3"),
           CalendarWeek(9, iso_week(date(2026, 7, 5)), date(2026, 7, 5), 2, "2026-07", "2026-Q3")]
    calendar = Calendar(cal, detect_convention(cal))
    # 6 Sep is two weeks back: offset 2 already belongs to 30 Aug.
    assert calendar.place(date(2026, 9, 3)) == "calendar_conflict"
