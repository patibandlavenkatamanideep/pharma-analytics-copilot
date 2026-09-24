"""A display cap must not decide what a comparison compares.

`ranking.limit` and the row cap are about how much of an answer to show. Applied
to the INPUT of a period comparison they change what the answer means: each
period is cut to its own top rows first, and the change is then computed
between two differently-truncated populations. The product with the largest
share movement is exactly the one likely to be missing from one of them.
"""

from __future__ import annotations

import re

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan

ANCHOR = {
    "min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
    "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3",
}


def compile_sql(plan_dict, *, max_rows=2):
    plan = AnalyticalPlan.model_validate(plan_dict)
    return Compiler(max_rows=max_rows).compile(plan, anchor=ANCHOR)


def cte_body(sql: str) -> str:
    """Everything before the outer SELECT that joins the two periods."""
    return sql[: sql.rindex("FROM cur c")]


TREND = {
    "metric": "share_trend_pp",
    "dimensions": ["product"],
    "time": {"kind": "named", "named": "r3m"},
    "comparison": {"kind": "named", "named": "r6m_prior"},
}


def test_no_row_cap_is_applied_inside_a_comparison():
    query = compile_sql(TREND)
    assert not re.findall(r"LIMIT \d+", cte_body(query.sql)), (
        "each period was truncated before the change was computed"
    )


def test_the_cap_still_applies_to_the_finished_comparison():
    """The fix must remove the cap from the inputs, not from the answer."""
    query = compile_sql(TREND)
    assert re.findall(r"LIMIT \d+", query.sql), "the display cap disappeared entirely"


def test_an_explicit_ranking_applies_after_the_change_is_computed():
    query = compile_sql({**TREND, "ranking": {"direction": "top", "limit": 5}})
    assert not re.findall(r"LIMIT \d+", cte_body(query.sql))
    tail = query.sql[query.sql.rindex("FROM cur c"):]
    assert "LIMIT 5" in tail
    assert "ORDER BY value DESC" in tail, "ranked by something other than the change"


def test_a_plain_ratio_still_gets_its_display_cap():
    """Only a comparison input is uncapped."""
    query = compile_sql({
        "metric": "brand_market_share",
        "dimensions": ["product"],
        "time": {"kind": "named", "named": "r3m"},
    })
    assert "LIMIT 3" in query.sql, "a standalone ratio lost its row cap"


def test_a_volume_comparison_is_also_uncapped_on_its_inputs():
    query = compile_sql({
        "metric": "volume_growth",
        "dimensions": ["account"],
        "time": {"kind": "named", "named": "r3m"},
        "comparison": {"kind": "named", "named": "r6m_prior"},
    })
    assert not re.findall(r"LIMIT \d+", cte_body(query.sql))


@pytest.mark.parametrize("dimension", ["product", "account", "territory"])
def test_comparison_joins_on_id_not_label(dimension):
    """Two organizations can share a name; a label is not an identity.

    Joining the two periods on the display label would merge distinct entities
    and split one entity that was renamed between periods.
    """
    query = compile_sql({
        "metric": "volume_growth",
        "dimensions": [dimension],
        "time": {"kind": "named", "named": "r3m"},
        "comparison": {"kind": "named", "named": "r6m_prior"},
    })
    join = query.sql[query.sql.rindex("FULL OUTER JOIN"):]
    on_clause = join[: join.index("\n")] if "\n" in join else join
    assert "dim0_id" in on_clause, on_clause
    assert "dim0_label" not in on_clause, f"joined on the label: {on_clause}"
