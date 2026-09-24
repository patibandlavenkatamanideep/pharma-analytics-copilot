"""Reporting period resolution.

Windows are anchored to the dataset's own reporting anchor (from the ingestion
manifest), never to the server clock, so the same question returns the same
number regardless of when it is asked.

Two traps the supplied documents set, both handled explicitly:

  * "R6M" is defined as the three months PRECEDING R3M (mo_offset 3,4,5), not
    six months. A literal "last six months" is offsets 0-5. These are different
    windows and the answer says which one it used.

  * "Last quarter" is defined as mo_offset IN (1,2,3), which at a September
    anchor means June-August -- not calendar Q2 (April-June). When a user could
    plausibly mean either, the caller asks rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.analytics.plan import NamedWindow, TimeWindow

# Named window -> (column, offsets). Sourced from docs/period_offsets.md and
# docs/metric_definitions.md; see docs/ASSUMPTIONS.md#a6.
NAMED_OFFSETS: dict[NamedWindow, tuple[str, list[int]]] = {
    NamedWindow.current_month: ("mo_offset", [0]),
    NamedWindow.last_month: ("mo_offset", [1]),
    NamedWindow.r3m: ("mo_offset", [0, 1, 2]),
    NamedWindow.r6m_prior: ("mo_offset", [3, 4, 5]),
    NamedWindow.last_6_months: ("mo_offset", [0, 1, 2, 3, 4, 5]),
    NamedWindow.last_quarter: ("mo_offset", [1, 2, 3]),
    NamedWindow.r30d: ("wk_offset", [0, 1, 2, 3]),
    NamedWindow.current_week: ("wk_offset", [0]),
}

NAMED_DESCRIPTION: dict[NamedWindow, str] = {
    NamedWindow.current_month: "the current (incomplete) reporting month",
    NamedWindow.last_month: "the most recently completed month",
    NamedWindow.r3m: "the rolling 3 months (current month plus the two prior)",
    NamedWindow.r6m_prior: "the 3 months preceding R3M (the business R6M comparison window)",
    NamedWindow.last_6_months: "the last six reporting months",
    NamedWindow.last_quarter: "the three months ending one month back (offsets 1-3), "
                              "which is the supplied definition of 'last quarter' and is "
                              "not the previous calendar quarter",
    NamedWindow.r30d: "the last four reporting weeks",
    NamedWindow.current_week: "the current reporting week",
    NamedWindow.ytd: "year to date, by reporting quarter labels",
    NamedWindow.all_time: "the full available history",
}

# Windows that include mo_offset 0 / wk_offset 0 cover a period that is still
# accumulating, which materially affects trends and must be disclosed.
INCLUDES_INCOMPLETE = {
    NamedWindow.current_month, NamedWindow.r3m, NamedWindow.last_6_months,
    NamedWindow.r30d, NamedWindow.current_week, NamedWindow.ytd,
}


@dataclass
class ResolvedWindow:
    """A window resolved to a parameterized predicate.

    `sql` contains only a fixed column name chosen from an allowlist and
    placeholders; every value travels as a bound parameter.
    """
    sql: str
    params: list[Any]
    label: str
    incomplete_period: bool = False
    caveats: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.caveats is None:
            self.caveats = []


class PeriodError(ValueError):
    pass


def resolve(
    window: TimeWindow,
    anchor: dict[str, Any],
    *,
    alias: str = "s",
) -> ResolvedWindow:
    """Resolve a plan time window against the dataset's reporting anchor."""
    if window.kind == "named":
        return _resolve_named(window.named, anchor, alias)
    if window.kind == "month_offsets":
        return _offsets("mo_offset", window.month_offsets or [], anchor, alias, "month")
    if window.kind == "week_offsets":
        return _offsets("wk_offset", window.week_offsets or [], anchor, alias, "week")
    if window.kind == "period_labels":
        return _labels(window.period_labels or [], alias)
    if window.kind == "date_range":
        return _dates(window.date_from, window.date_to, alias)
    raise PeriodError(f"unsupported window kind {window.kind!r}")


def _resolve_named(
    named: NamedWindow | None, anchor: dict[str, Any], alias: str
) -> ResolvedWindow:
    if named is None:
        raise PeriodError("named window missing")

    if named is NamedWindow.all_time:
        return ResolvedWindow(sql="TRUE", params=[], label="all available history")

    if named is NamedWindow.ytd:
        # Year to date uses labels, since offsets do not encode a calendar year.
        max_qtr = str(anchor.get("max_period_qtr") or "")
        if not max_qtr:
            raise PeriodError("cannot resolve year to date without a reporting anchor")
        year = max_qtr.split("-")[0]
        return ResolvedWindow(
            sql=f"{alias}.period_qtr LIKE %s",
            params=[f"{year}-Q%"],
            label=f"year to date ({year})",
            incomplete_period=True,
            caveats=[f"{year} is still in progress; the latest quarter is incomplete."],
        )

    column, offsets = NAMED_OFFSETS[named]
    available = _available(column, anchor)
    usable = [o for o in offsets if o in available] if available else offsets
    caveats: list[str] = []
    if available and len(usable) < len(offsets):
        missing = sorted(set(offsets) - set(usable))
        caveats.append(
            f"The dataset has no data for {column} {missing}, so this window is "
            "shorter than its definition."
        )
    if not usable:
        raise PeriodError(
            f"the requested window ({named.value}) lies entirely outside the "
            f"available reporting range"
        )

    return ResolvedWindow(
        sql=f"{alias}.{column} = ANY(%s)",
        params=[usable],
        label=f"{named.value.replace('_', ' ')} — {NAMED_DESCRIPTION[named]}",
        incomplete_period=named in INCLUDES_INCOMPLETE,
        caveats=caveats,
    )


def _available(column: str, anchor: dict[str, Any]) -> set[int]:
    lo = anchor.get("min_mo" if column == "mo_offset" else "min_wk")
    hi = anchor.get("max_mo" if column == "mo_offset" else "max_wk")
    if lo is None or hi is None:
        return set()
    return set(range(int(lo), int(hi) + 1))


def _offsets(
    column: str, offsets: list[int], anchor: dict[str, Any], alias: str, unit: str
) -> ResolvedWindow:
    if not offsets:
        raise PeriodError(f"no {unit} offsets supplied")
    available = _available(column, anchor)
    if available and not set(offsets) & available:
        raise PeriodError(
            f"{unit} offsets {sorted(offsets)} lie outside the available range "
            f"{min(available)}-{max(available)}"
        )
    return ResolvedWindow(
        sql=f"{alias}.{column} = ANY(%s)",
        params=[sorted(set(offsets))],
        label=f"{unit} offsets {sorted(set(offsets))}",
        incomplete_period=0 in offsets,
    )


def _labels(labels: list[str], alias: str) -> ResolvedWindow:
    if not labels:
        raise PeriodError("no period labels supplied")
    # The label's shape decides which column it belongs to; mixing shapes in one
    # window would silently drop rows.
    kinds = {_label_kind(x) for x in labels}
    if len(kinds) > 1:
        raise PeriodError(f"cannot mix period label types {sorted(kinds)} in one window")
    column = kinds.pop()
    return ResolvedWindow(
        sql=f"{alias}.{column} = ANY(%s)",
        params=[sorted(set(labels))],
        label=f"{column.replace('period_', '')} {', '.join(sorted(set(labels)))}",
    )


def _label_kind(label: str) -> str:
    if "-Q" in label:
        return "period_qtr"
    if "-W" in label:
        return "period_wk"
    return "period_mo"


def _dates(date_from: str | None, date_to: str | None, alias: str) -> ResolvedWindow:
    if not (date_from and date_to):
        raise PeriodError("a date range needs both ends")
    if date_from > date_to:
        raise PeriodError("date range starts after it ends")
    # Only an explicit calendar-day request uses transaction_date. Because
    # periods follow the week-ending month, a date range and an offset window
    # are not interchangeable -- 186,700 generated rows differ between them.
    return ResolvedWindow(
        sql=f"{alias}.transaction_date >= %s AND {alias}.transaction_date <= %s",
        params=[date_from, date_to],
        label=f"transaction dates {date_from} to {date_to}",
        caveats=[
            "Filtered on calendar transaction dates. Reporting periods follow the "
            "week-ending month, so these totals can differ from the equivalent "
            "reporting-period window."
        ],
    )
