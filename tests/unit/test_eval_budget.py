"""A live evaluation spends real money, so it is bounded before it starts;
and a holdout is only a holdout if it was frozen before anyone saw answers.

No model is called: the budget is exercised with recorded usage shapes, and
the refusals happen before any database or provider is touched.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("run_evals", ROOT / "scripts" / "run_evals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def usage(i, o):
    from app.llm.planner import TokenUsage
    return TokenUsage(input_tokens=i, output_tokens=o)


def unreported():
    from app.llm.planner import TokenUsage
    return TokenUsage()


def test_reported_usage_is_charged_as_reported(ev):
    b = ev.Budget(100_000, 10_000)
    for u in (usage(4_670, 160), usage(4_500, 300), usage(4_900, 150)):
        assert b.allow_call()
        b.record_call(u)
    assert (b.input, b.output, b.calls, b.unreported_calls) == (14_070, 610, 3, 0)


def test_an_unreported_call_is_charged_at_the_ceiling_never_zero(ev):
    b = ev.Budget(100_000, 100_000)
    b.record_call(unreported())
    assert (b.input, b.output, b.unreported_calls) == (8_000, 4_096, 1)


def test_a_call_is_allowed_only_if_one_more_at_the_ceiling_fits(ev):
    b = ev.Budget(10_000, 10_000)
    assert b.allow_call()                         # 8,000 <= 10,000
    b.record_call(usage(4_670, 160))
    assert not b.allow_call()                     # 4,670 + 8,000 > 10,000
    assert b.refused_calls == 1


def test_the_next_question_needs_room_for_a_plan_and_a_repair(ev):
    b = ev.Budget(20_000, 10_000)
    assert b.can_afford_another()                 # 2 x 8,000 = 16,000 <= 20,000
    b.record_call(usage(4_670, 160))
    assert not b.can_afford_another()             # 4,670 + 16,000 > 20,000


def test_whatever_the_calls_report_the_caps_hold(ev):
    """Usage within the per-call ceilings, reported or not, in any order:
    the charged total never exceeds either cap."""
    import random
    rng = random.Random(7)
    for _ in range(200):
        b = ev.Budget(rng.randint(5_000, 60_000), rng.randint(4_096, 20_000))
        while b.allow_call():
            b.record_call(unreported() if rng.random() < 0.3
                          else usage(rng.randint(0, 8_000), rng.randint(0, 4_096)))
            assert b.input <= b.max_input and b.output <= b.max_output


def test_a_call_above_the_ceiling_raises_the_ceiling(ev):
    """The input ceiling is an estimate. A call that exceeds it can overshoot
    the cap once, by that excess; the ceiling then rises to it, so the next
    allowance accounts for prompts that size."""
    b = ev.Budget(50_000, 50_000)
    b.record_call(usage(9_500, 100))
    assert b.input_ceiling == 9_500


# -- the planner honours the meter --------------------------------------------------

def planner_with(replies):
    from tests.unit.test_live_adapter_contract import make_planner
    return make_planner(replies)


def test_a_repair_the_budget_cannot_cover_is_never_made(ev):
    from app.llm.planner import PlannerBudgetExhausted
    from tests.unit.test_live_adapter_contract import (
        FakeBlock, FakeResponse, FakeUsage, context_with, valid_plan_block,
    )

    budget = ev.Budget(12_000, 10_000)              # room for exactly one call
    planner = planner_with([
        FakeResponse([FakeBlock(type="tool_use", name="emit_plan",
                                input={"metric": "not_a_metric"})], FakeUsage(4_670, 160)),
        FakeResponse([valid_plan_block()], FakeUsage(4_670, 160)),
    ])
    ctx = context_with("top accounts")
    ctx.spend = budget
    with pytest.raises(PlannerBudgetExhausted):
        planner.plan("top accounts", ctx)
    assert len(planner._client.messages.requests) == 1
    assert (budget.calls, budget.input, budget.refused_calls) == (1, 4_670, 1)


def test_sdk_retries_are_off_while_metered_and_kept_otherwise(ev):
    import time as _time
    from tests.unit.test_live_adapter_contract import FakeResponse, context_with, valid_plan_block

    metered = planner_with([FakeResponse([valid_plan_block()], None)] * 2)
    for deadline in (None, _time.time() + 60):
        ctx = context_with("top accounts")
        ctx.spend, ctx.deadline_at = ev.Budget(100_000, 100_000), deadline
        metered.plan("top accounts", ctx)
    assert [o["max_retries"] for o in metered._client.options] == [0, 0]

    unmetered = planner_with([FakeResponse([valid_plan_block()], None)])
    ctx = context_with("top accounts")
    ctx.deadline_at = _time.time() + 120
    unmetered.plan("top accounts", ctx)
    assert unmetered._client.options[0]["max_retries"] == 2


def test_the_smoke_subset_is_one_question_per_family(ev):
    qs = [{"id": "a1", "family": "a"}, {"id": "a2", "family": "a"},
          {"id": "b1", "family": "b"}, {"id": "c1"}]
    assert [q["id"] for q in ev.smoke_subset(qs)] == ["a1", "b1", "c1"]


@pytest.mark.parametrize("name", ["questions.yaml", "holdout.yaml", "holdout2.yaml"])
def test_the_existing_sets_say_what_they_are(ev, name):
    import yaml
    path = ROOT / "evals" / name
    ok, status = ev.runnable(path, yaml.safe_load(path.read_text()))
    assert ok and status in ("regression", "spent")


def test_a_holdout_runs_only_as_frozen(ev, tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "FROZEN", tmp_path / "frozen.json")
    holdout = tmp_path / "holdout9.yaml"
    holdout.write_text('status: "holdout"\nversion: "1"\nquestions: []\n')
    spec = {"status": "holdout"}
    ok, why = ev.runnable(holdout, spec)
    assert not ok and "not been frozen" in why

    (tmp_path / "frozen.json").write_text(json.dumps({"holdout9.yaml": {
        "sha256": ev.file_sha256(holdout), "frozen_at": "2026-10-01T00:00:00+00:00"}}))
    assert ev.runnable(holdout, spec) == (True, "holdout")

    holdout.write_text('status: "holdout"\nversion: "1"\nquestions: [{id: changed}]\n')
    ok, why = ev.runnable(holdout, spec)
    assert not ok and "changed after it was frozen" in why


def test_a_set_with_no_status_is_refused(ev, tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("questions: []\n")
    assert ev.runnable(path, {})[0] is False


def test_a_live_run_without_a_budget_is_refused_before_anything_starts(ev, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_evals.py", "--provider", "bedrock"])
    assert ev.main() == 2
    assert "needs a budget" in capsys.readouterr().err


# -- the cap is checked against the request actually sent ---------------------------
#
# Review of 1 October 2026, R3. allow_call() compared the cap with a fixed
# 8,000-token estimate, never with the request about to be sent; the output
# side assumed 4,096 whatever max_tokens was configured; a larger call was
# charged afterwards and the estimate raised. So the cap could be exceeded.

R3 = pytest.mark.xfail(strict=True, reason="R3: the evaluation cap is checked against an "
                                           "estimate, not the request being sent")


def long_catalog_context(entries: int = 1_000):
    """A planning context whose prompt is far larger than any fixed estimate:
    the vocabulary a large customer's catalogue puts in every request."""
    from tests.unit.test_live_adapter_contract import context_with
    return context_with("top accounts", known_gpos=[
        f"Group Purchasing Organisation {i:04d}" for i in range(entries)])


@R3
def test_a_first_call_larger_than_the_cap_is_never_sent(ev):
    """The review's reproduction: a 16,000-token input cap admitted a call
    that then billed 17,000. A request that cannot be shown to fit is not sent."""
    from app.llm.planner import PlannerBudgetExhausted
    from tests.unit.test_live_adapter_contract import FakeResponse, FakeUsage, valid_plan_block

    budget = ev.Budget(16_000, 8_192)
    planner = planner_with([FakeResponse([valid_plan_block()], FakeUsage(17_000, 160))])
    ctx = long_catalog_context()
    ctx.spend = budget
    with pytest.raises(PlannerBudgetExhausted):
        planner.plan("top accounts", ctx)
    assert planner._client.messages.requests == []
    assert (budget.input, budget.output) == (0, 0)


@R3
@pytest.mark.parametrize("max_tokens,output_cap,sent", [
    (8_192, 6_000, False),      # the response may be 8,192 tokens: does not fit
    (1_024, 1_500, True),       # at most 1,024: fits, whatever a fixed 4,096 says
])
def test_the_output_reservation_is_the_configured_max_tokens(ev, monkeypatch, max_tokens,
                                                              output_cap, sent):
    from app.llm.planner import PlannerBudgetExhausted
    from tests.unit.test_live_adapter_contract import (
        FakeResponse, FakeUsage, context_with, valid_plan_block,
    )

    planner = planner_with([FakeResponse([valid_plan_block()], FakeUsage(4_670, 160))])
    monkeypatch.setattr(planner.settings, "llm_max_tokens", max_tokens)
    ctx = context_with("top accounts")
    ctx.spend = ev.Budget(10**6, output_cap)
    if sent:
        planner.plan("top accounts", ctx)
        assert [r["max_tokens"] for r in planner._client.messages.requests] == [max_tokens]
    else:
        with pytest.raises(PlannerBudgetExhausted):
            planner.plan("top accounts", ctx)
        assert planner._client.messages.requests == []


@R3
def test_a_call_with_unreported_usage_is_charged_at_least_its_own_size(ev):
    """Unknown usage is charged conservatively: never less than the request
    that was sent could have cost, and the full output it was allowed."""
    from tests.unit.test_live_adapter_contract import FakeResponse, valid_plan_block

    planner = planner_with([FakeResponse([valid_plan_block()], None)])
    ctx = long_catalog_context()
    budget = ev.Budget(10**6, 10**6)
    ctx.spend = budget
    planner.plan("top accounts", ctx)
    request = planner._client.messages.requests[0]
    assert budget.input >= len(request["system"].encode())
    assert budget.output == request["max_tokens"]


@R3
def test_usage_above_the_preflight_bound_stops_the_run(ev):
    """If a call ever bills more than its reservation, the bound was wrong:
    the run stops rather than carry on with a cap it cannot keep."""
    from app.llm.planner import PlannerBudgetExhausted
    from tests.unit.test_live_adapter_contract import (
        FakeResponse, FakeUsage, context_with, valid_plan_block,
    )

    planner = planner_with([FakeResponse([valid_plan_block()], FakeUsage(10_000_000, 160))])
    ctx = context_with("top accounts")
    ctx.spend = ev.Budget(10**8, 10**6)
    with pytest.raises(PlannerBudgetExhausted):
        planner.plan("top accounts", ctx)
