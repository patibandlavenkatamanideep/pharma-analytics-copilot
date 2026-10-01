"""A planner that does whatever an injected instruction told it to.

A live model can be talked into emitting a plan its user may not have --
pricing for a RAM, another territory's accounts, a cohort it was never
shown. Prompt wording is not the control; the policy, the compiler and the
database are. This planner stands in for a model that has been fully
persuaded, so tests can show that being persuaded buys nothing.

It records every question and context it was given, so a test can also
show what the model would have been told.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import ValidationError

from app.analytics.plan import AnalyticalPlan
from app.llm.planner import (
    PlannerError, PlanningAttempt, PlanningContext, PlanningResult, TokenUsage,
)


class HostilePlanner:
    def __init__(self, plan: dict[str, Any] | Callable[[str, PlanningContext], dict[str, Any]]):
        self._plan = plan
        self.seen: list[tuple[str, PlanningContext]] = []

    def plan(self, question: str, context: PlanningContext) -> PlanningResult:
        self.seen.append((question, context))
        raw = self._plan(question, context) if callable(self._plan) else self._plan
        try:
            plan = AnalyticalPlan.model_validate(raw)
        except ValidationError as exc:
            # What the live adapter does once its repair also fails: the
            # request ends as a planner error, and nothing is compiled.
            raise PlannerError(f"invalid plan: {exc.error_count()} errors") from None
        return PlanningResult(
            plan=plan, provider="hostile",
            model_id="hostile-test-double", prompt_version="test",
            planner_contract_version="test",
            attempts=(PlanningAttempt(ordinal=1, kind="initial", outcome="plan",
                                      usage=TokenUsage(), error=None),))


def pipeline_with(plan) -> tuple[Any, HostilePlanner]:
    from app.pipeline import Pipeline

    planner = HostilePlanner(plan)
    return Pipeline(planner), planner
