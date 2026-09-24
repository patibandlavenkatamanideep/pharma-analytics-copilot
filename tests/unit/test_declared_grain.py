"""The declared grain is a promise about the rows, and it is checked.

`plan.dimensions` is what the answer says it is broken down by. If the rows do
not honour it -- the same group appearing twice because some join fanned out --
the table reads as more groups than exist, and any subtotal a person adds up by
eye is wrong. There is no honest way to render that, so it fails closed.
"""

from __future__ import annotations

import pytest

from app.analytics.plan import AnalyticalPlan
from app.analytics.render import GrainError, _assert_declared_grain


def plan_with(dimensions):
    return AnalyticalPlan.model_validate({
        "metric": "paid_pack_units",
        "dimensions": dimensions,
        "time": {"kind": "named", "named": "r3m"},
    })


def test_a_repeated_group_is_refused():
    rows = [
        {"dim0_id": "ORG-1", "dim0_label": "Acme", "value": 10},
        {"dim0_id": "ORG-2", "dim0_label": "Beta", "value": 5},
        {"dim0_id": "ORG-1", "dim0_label": "Acme", "value": 7},
    ]
    with pytest.raises(GrainError, match="duplicate group"):
        _assert_declared_grain(rows, plan_with(["account"]))


def test_distinct_groups_are_fine():
    rows = [
        {"dim0_id": "ORG-1", "dim0_label": "Acme", "value": 10},
        {"dim0_id": "ORG-2", "dim0_label": "Beta", "value": 5},
    ]
    _assert_declared_grain(rows, plan_with(["account"]))


def test_the_whole_tuple_is_the_grain_not_the_first_column():
    """Same account, different months is two groups, not a duplicate."""
    rows = [
        {"dim0_id": "ORG-1", "dim1_id": "2026-08", "value": 1},
        {"dim0_id": "ORG-1", "dim1_id": "2026-09", "value": 2},
    ]
    _assert_declared_grain(rows, plan_with(["account", "period_mo"]))

    rows.append({"dim0_id": "ORG-1", "dim1_id": "2026-09", "value": 3})
    with pytest.raises(GrainError):
        _assert_declared_grain(rows, plan_with(["account", "period_mo"]))


def test_two_entities_sharing_a_label_are_two_groups():
    """Identity is the id. Two organizations may legitimately share a name."""
    rows = [
        {"dim0_id": "ORG-1", "dim0_label": "Mercy Hospital", "value": 10},
        {"dim0_id": "ORG-2", "dim0_label": "Mercy Hospital", "value": 4},
    ]
    _assert_declared_grain(rows, plan_with(["account"]))


def test_an_ungrouped_query_returning_many_rows_is_refused():
    rows = [{"value": 1}, {"value": 2}]
    with pytest.raises(GrainError, match="no dimensions"):
        _assert_declared_grain(rows, plan_with([]))


def test_a_null_group_is_still_a_group():
    rows = [{"dim0_id": None, "value": 1}, {"dim0_id": None, "value": 2}]
    with pytest.raises(GrainError, match="duplicate group"):
        _assert_declared_grain(rows, plan_with(["account"]))


# ---------------------------------------------------------------------------
# The plan itself
# ---------------------------------------------------------------------------

def test_a_plan_cannot_declare_the_same_dimension_twice():
    with pytest.raises(ValueError, match="distinct"):
        plan_with(["account", "account"])


def test_distinct_dimensions_are_accepted():
    assert len(plan_with(["account", "product"]).dimensions) == 2
