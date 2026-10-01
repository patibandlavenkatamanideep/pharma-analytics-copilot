"""A plan must have the shape the question asked for.

"Top 20 facilities by paid pack units" was answered with one company-wide
total -- a true number, the wrong answer -- and no check noticed. These pin
what is read from a question, and what each kind of mismatch does:
disclosed when the figure is true but not the whole question, refused when
it is the opposite answer.
"""

from __future__ import annotations

import pytest

from app.analytics.entities import Vocabulary
from app.analytics.intent import blocking, find_gaps
from app.analytics.plan import AnalyticalPlan
from app.analytics.structure import asked_shape

VOCAB = Vocabulary(products=["ZENOVAX"], territories=["New York Metro"],
                   all_territories=["New York Metro"], all_regions=["Northeast"])
LAST_Q = {"kind": "named", "named": "last_quarter"}


def plan(**kw) -> AnalyticalPlan:
    return AnalyticalPlan.model_validate({"metric": "paid_pack_units", "time": LAST_Q, **kw})


def shape_gaps(question, p):
    kinds = {"grouping_dropped", "ranking_dropped", "ranking_limit_changed",
             "ranking_direction_mismatch"}
    return [g for g in find_gaps(question, p, VOCAB) if g.kind in kinds]


@pytest.mark.parametrize("question, groupings, ranking", [
    ("top 20 facilities by paid pack units last 6 months", [], ("facilities", "top", 20)),
    ("paid pack units by facility and month, all time", ["facility", "month"], None),
    ("which health systems have the most facilities", [], ("health systems", "top", None)),
    ("bottom 5 accounts by paid pack units", [], ("accounts", "bottom", 5)),
    ("which territories had the lowest share", [], ("territories", "bottom", None)),
    ("monthly volume for ZENOVAX", ["monthly"], None),
    ("top five products by territory", ["territory"], ("products", "top", 5)),
    ("volume by region and territory last month", ["region", "territory"], None),
    ("highest volume products last quarter", [], ("products", "top", None)),
    # Not a breakdown, not a ranking:
    ("average paid pack units per facility", [], None),
    ("volume in the most recent month", [], None),
    ("most accounts grew last quarter", [], None),
    ("top 10 accounts by paid pack units", [], ("accounts", "top", 10)),
    ("what was total paid pack units last quarter", [], None),
])
def test_what_is_read_from_the_question(question, groupings, ranking):
    shape = asked_shape(question)
    assert [w for w, _ in shape.groupings] == groupings
    got = shape.ranking and (shape.ranking.word, shape.ranking.direction, shape.ranking.limit)
    assert got == ranking


def test_a_total_for_a_ranking_is_disclosed():
    gaps = shape_gaps("top 20 facilities by paid pack units last quarter", plan())
    assert [g.kind for g in gaps] == ["ranking_dropped"]
    assert not blocking(gaps)


def test_a_different_top_n_is_disclosed():
    gaps = shape_gaps("top 20 facilities by paid pack units last quarter",
                      plan(dimensions=["facility"], ranking={"direction": "top", "limit": 10}))
    assert [g.kind for g in gaps] == ["ranking_limit_changed"]
    assert "the top 10" in gaps[0].message()


def test_the_opposite_end_of_a_ranking_is_refused():
    gaps = shape_gaps("bottom 5 accounts by paid pack units last quarter",
                      plan(dimensions=["account"], ranking={"direction": "top", "limit": 5}))
    assert [g.kind for g in blocking(gaps)] == ["ranking_direction_mismatch"]


def test_the_lowest_without_a_ranking_is_disclosed():
    gaps = shape_gaps("which territories had the lowest paid pack units last quarter",
                      plan(dimensions=["territory"]))
    assert [g.kind for g in gaps] == ["ranking_dropped"]
    assert "largest first" in gaps[0].message()


def test_a_dropped_breakdown_is_disclosed():
    gaps = shape_gaps("paid pack units by facility and month, all time",
                      plan(dimensions=["facility"]))
    assert [g.kind for g in gaps] == ["grouping_dropped"]
    assert "month" in gaps[0].message()


def test_top_periods_listed_in_time_order_are_disclosed():
    gaps = shape_gaps("show the top 3 months by volume", plan(dimensions=["period_mo"]))
    assert [g.kind for g in gaps] == ["ranking_dropped"]
    assert "in time order" in gaps[0].message()


@pytest.mark.parametrize("question, kw", [
    ("which facilities had the most paid pack units last quarter", {"dimensions": ["facility"]}),
    ("top 10 accounts by paid pack units last quarter",
     {"dimensions": ["account"], "ranking": {"direction": "top", "limit": 10}}),
    ("top 10 health systems last quarter",
     {"dimensions": ["parent"], "ranking": {"direction": "top", "limit": 10}}),
    ("paid pack units by facility and month",
     {"dimensions": ["facility", "period_mo"]}),
    ("average paid pack units per facility last quarter", {}),
    ("total paid pack units last quarter", {}),
])
def test_a_plan_with_the_asked_shape_has_no_shape_gap(question, kw):
    assert shape_gaps(question, plan(**kw)) == []
