"""Pricing cannot leak through any shape the plan can take.

Review Phase 4: "Test protected values through selection, filtering,
ranking, comparison, history, cache, graph resume, traces and exports."
SQL-level attacks on sales.wac are covered in test_access_control.py --
twelve statement shapes, refused by both the database role and the
validator. This covers the plan space above them, exhaustively:

* every metric that needs WAC, in every shape -- breakdown, ranking,
  threshold, comparison, rolling series -- is refused by the policy for
  every principal without pricing;
* every metric that does NOT need WAC, in every shape, compiles to SQL
  that never names the column, so a non-pricing principal's query passes
  the validator and the database role alike -- including the
  calendar-backed series and a frozen cohort;
* end to end, a RAM asking for revenue in ranking, threshold and growth
  phrasings gets no currency anywhere in the response.

History is covered in test_session_authorization.py (losing pricing hides
earlier turns), graph resume in test_graph_durability.py, idempotent replay
in test_conversation_reliability.py. There is no result cache to cover.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import CohortBinding, Compiler, UnsupportedCombination
from app.analytics.plan import AnalyticalPlan, MetricKey
from app.analytics.registry import get_registry
from app.analytics.validator import validate
from app.auth.policy import AuthorizationError, authorize, build_principal

pytestmark = pytest.mark.security

R3M = {"kind": "named", "named": "r3m"}
PRIOR = {"kind": "named", "named": "r6m_prior"}
ANCHOR = {"min_mo": 0, "max_mo": 36, "min_wk": 0, "max_wk": 155,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}

SHAPES = {
    "total": {},
    "by_account": {"dimensions": ["account"]},
    "ranked": {"dimensions": ["account"], "ranking": {"direction": "top", "limit": 5}},
    "thresholded": {"dimensions": ["account"], "threshold": {"op": "gte", "value": 100}},
    "rolling": {"dimensions": ["period_mo"], "rolling": {"periods": 3},
                "time": {"kind": "named", "named": "last_6_months"}},
    "by_product_and_month": {"dimensions": ["product", "period_mo"]},
}

NO_PRICING = {
    "ram": dict(role="ram", territory_name="New York Metro", region_name="Northeast",
                can_view_wac=0),
    "director": dict(role="director", territory_name=None, region_name="Northeast",
                     can_view_wac=0),
    "exec_without_flag": dict(role="exec", territory_name=None, region_name=None,
                              can_view_wac=0),
}


def principal(kind):
    return build_principal({"user_id": f"P-{kind}", "email": f"{kind}@x", "full_name": kind,
                            **NO_PRICING[kind]})


def plan_for(metric: MetricKey, shape: dict) -> AnalyticalPlan | None:
    spec = get_registry().get(metric.value)
    body = {"metric": metric.value, "time": R3M, **shape}
    if spec.get("kind") == "period_change":
        body["comparison"] = PRIOR
    try:
        return AnalyticalPlan.model_validate(body)
    except ValueError:
        return None                               # the shape cannot be expressed


PRICED = [m for m in MetricKey if get_registry().requires_wac(m.value)]
UNPRICED = [m for m in MetricKey if not get_registry().requires_wac(m.value)]


def test_there_is_at_least_one_priced_metric():
    assert PRICED, "nothing requires WAC -- this matrix would test nothing"


@pytest.mark.parametrize("who", sorted(NO_PRICING))
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("metric", PRICED, ids=lambda m: m.value)
def test_a_priced_metric_is_refused_in_every_shape(metric, shape, who):
    plan = plan_for(metric, SHAPES[shape])
    if plan is None:
        pytest.fail(f"{metric.value} cannot take the {shape} shape; adjust SHAPES")
    with pytest.raises(AuthorizationError):
        authorize(plan, principal(who))


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("metric", UNPRICED, ids=lambda m: m.value)
def test_an_unpriced_metric_never_names_the_column(metric, shape):
    plan = plan_for(metric, SHAPES[shape])
    if plan is None:
        return                                    # inexpressible, so nothing to leak
    try:
        q = Compiler(max_rows=5000).compile(plan, anchor=ANCHOR)
    except UnsupportedCombination:
        return                                    # refused by name, nothing compiled
    assert "wac" not in q.sql.lower()
    validate(q.sql, wac_authorized=False)


@pytest.mark.parametrize("metric", UNPRICED, ids=lambda m: m.value)
def test_a_frozen_cohort_does_not_open_a_pricing_path(metric):
    plan = plan_for(metric, {"dimensions": ["account"]})
    try:
        q = Compiler(max_rows=5000).compile(
            plan, anchor=ANCHOR, cohort=CohortBinding("account", ("GP001", "GP002")))
    except UnsupportedCombination:
        return
    assert "wac" not in q.sql.lower()
    validate(q.sql, wac_authorized=False)


@pytest.mark.parametrize("question", [
    "What is my total revenue in dollars this quarter?",
    "Rank my accounts by revenue this quarter",
    "Which accounts had revenue above $100,000 this quarter?",
    "How did revenue grow versus the prior quarter?",
    "Show me WAC by product",
])
def test_a_ram_never_sees_currency_whatever_the_phrasing(client, make_identity, real_scopes,
                                                         question):
    from tests.security.helpers import sign_in
    sign_in(client, make_identity("ram", territory=real_scopes[0]["territory_name"],
                                  region=real_scopes[0]["region_name"]))
    r = client.post("/api/ask", json={"question": question, "include_sql": True})
    assert r.status_code == 200
    body = r.json()
    assert "$" not in r.text, f"currency reached a RAM: {body.get('message')}"
    assert "wac" not in (body.get("sql") or "").lower()
