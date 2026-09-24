"""Turn result rows into an answer.

Every number in the response is formatted here from the actual query result.
No language model is asked to restate a figure, so a number cannot drift
between what the database returned and what the user reads.

Data-quality checks run on the values themselves, so an impossible ratio is
labelled from the evidence rather than from a static note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.analytics.compiler import CompiledQuery
from app.analytics.plan import AnalyticalPlan, MetricKey


@dataclass
class Answer:
    headline: str
    table: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    scope_note: str = ""
    period_note: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


def format_value(value: Any, unit: str) -> str:
    if value is None:
        return "unavailable"
    if unit == "USD":
        return f"${value:,.2f}"
    if unit == "ratio":
        return f"{value * 100:.2f}%"
    if unit == "percentage points":
        return f"{value:+.2f} pp"
    if unit in ("packs", "equivalents"):
        # Pack counts are whole in this data; equivalents carry a fraction.
        if abs(value - round(value)) < 1e-9:
            return f"{round(value):,} {unit}"
        return f"{value:,.2f} {unit}"
    if unit in ("accounts", "facilities"):
        return f"{round(value):,} {unit}"
    if isinstance(value, float):
        return f"{value:,.4f}"
    return f"{value:,}"


def _dimension_labels(plan: AnalyticalPlan) -> list[str]:
    from app.analytics.compiler import DIMENSIONS
    return [DIMENSIONS[d].description for d in plan.dimensions]


def check_quality(
    rows: list[dict[str, Any]], query: CompiledQuery, plan: AnalyticalPlan
) -> list[str]:
    """Data-quality findings derived from the returned values."""
    warnings: list[str] = []

    if "denominator_completeness" in query.quality_checks:
        # A3: market_data holds only competitor rows in the supplied data, so
        # the denominator is not the total market it is documented to be.
        warnings.append(
            "Market share here divides our distributor volume by the reported market "
            "total. In this dataset the market source contains only competitor "
            "products, so the denominator understates the true total market. Treat "
            "these percentages as indicative of relative position, not as a "
            "reliable share."
        )

    if "ratio_above_one" in query.quality_checks:
        impossible = [
            r for r in rows
            if r.get("value") is not None and isinstance(r.get("value"), (int, float))
            and r["value"] > 1.0
        ]
        if impossible:
            if plan.dimensions:
                sample = ", ".join(
                    str(r.get("dim0_label") or r.get("dim0_id")) for r in impossible[:4]
                )
                more = f" and {len(impossible) - 4} more" if len(impossible) > 4 else ""
                warnings.append(
                    f"{len(impossible)} of {len(rows)} rows exceed 100% ({sample}{more}). "
                    "A share above 100% is not a real share — it means the reported "
                    "market volume is smaller than our own shipments, so these are "
                    "inconsistent source volumes rather than a measurement."
                )
            else:
                warnings.append(
                    "This value exceeds 100%, which is not a real market share. It means "
                    "the reported market volume is smaller than our own shipments, so "
                    "the sources are inconsistent rather than the share being genuine."
                )

    missing_denominator = [
        r for r in rows if "denominator" in r and (r.get("denominator") in (None, 0))
    ]
    if missing_denominator:
        warnings.append(
            f"{len(missing_denominator)} row(s) have no reported market volume, so their "
            "share is shown as unavailable rather than as zero."
        )

    # A11: absent observations are not a verified zero.
    if not rows:
        warnings.append(
            "No matching records were found. That means no data was reported for this "
            "combination, which is not the same as a confirmed zero."
        )

    return warnings


def render(
    rows: list[dict[str, Any]],
    query: CompiledQuery,
    plan: AnalyticalPlan,
    *,
    scope_note: str,
    max_rows: int,
) -> Answer:
    truncated = len(rows) > max_rows
    shown = rows[:max_rows]

    period = f"Reporting window: {query.window_label}."
    if query.comparison_label:
        period += f" Compared with: {query.comparison_label}."

    headline = _headline(shown, query, plan)

    table: list[dict[str, Any]] = []
    for row in shown:
        item: dict[str, Any] = {}
        for i, _ in enumerate(plan.dimensions):
            label = row.get(f"dim{i}_label")
            item[f"dim{i}"] = label if label is not None else "(none)"
            item[f"dim{i}_id"] = row.get(f"dim{i}_id")
        if "numerator" in row:
            item["numerator"] = row["numerator"]
            item["denominator"] = row["denominator"]
        if "current_value" in row:
            item["current"] = row["current_value"]
            item["prior"] = row["prior_value"]
        item["value"] = row.get("value")
        item["value_formatted"] = format_value(row.get("value"), query.unit)
        table.append(item)

    return Answer(
        headline=headline,
        table=table,
        columns=[*_dimension_labels(plan), query.metric_label],
        scope_note=scope_note,
        period_note=period,
        warnings=check_quality(rows, query, plan),
        notes=list(query.notes),
        row_count=len(rows),
        truncated=truncated,
    )


def _headline(rows: list[dict[str, Any]], query: CompiledQuery, plan: AnalyticalPlan) -> str:
    metric = query.metric_label

    if not rows:
        return f"No {metric} was reported for this request."

    if not plan.dimensions:
        value = rows[0].get("value")
        text = format_value(value, query.unit)
        if plan.metric is MetricKey.brand_market_share and value is not None:
            num = format_value(rows[0].get("numerator"), "equivalents")
            den = format_value(rows[0].get("denominator"), "equivalents")
            return f"{metric.capitalize()} is {text} ({num} against a reported market of {den})."
        return f"{metric.capitalize()}: {text}."

    grain = _dimension_labels(plan)[0]
    if plan.ranking:
        direction = "Top" if plan.ranking.direction == "top" else "Bottom"
        lead = rows[0]
        label = lead.get("dim0_label") or lead.get("dim0_id") or "(none)"
        return (
            f"{direction} {len(rows)} by {grain}, ranked on {metric}. "
            f"{label} leads with {format_value(lead.get('value'), query.unit)}."
        )
    return f"{metric.capitalize()} by {grain}, {len(rows)} row(s)."
