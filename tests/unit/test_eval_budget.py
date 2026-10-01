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
        assert b.reserve(18_000, 4_096)
        b.record_call(u, (18_000, 4_096))
    assert (b.input, b.output, b.calls, b.unreported_calls) == (14_070, 610, 3, 0)
    assert not b.violated


def test_an_unreported_call_is_charged_its_whole_reservation_never_zero(ev):
    b = ev.Budget(100_000, 100_000)
    b.record_call(unreported(), (18_500, 2_048))
    assert (b.input, b.output, b.unreported_calls) == (18_500, 2_048, 1)


def test_a_call_is_sent_only_if_its_own_bound_fits_what_is_left(ev):
    b = ev.Budget(30_000, 10_000)
    assert b.reserve(18_000, 4_096)
    b.record_call(usage(4_670, 160), (18_000, 4_096))
    assert b.reserve(18_000, 4_096)                 # 4,670 + 18,000 <= 30,000
    b.record_call(usage(4_670, 160), (18_000, 4_096))
    assert not b.reserve(21_000, 4_096)             # 9,340 + 21,000 > 30,000
    assert b.refused_calls == 1


def test_the_next_question_needs_room_for_a_plan_and_a_repair(ev):
    b = ev.Budget(40_000, 10_000)
    assert b.can_afford_another()                   # nothing reserved yet
    assert b.reserve(18_000, 4_096)
    b.record_call(usage(4_670, 160), (18_000, 4_096))
    assert not b.can_afford_another()               # 4,670 + 2 x 18,000 > 40,000


def test_whatever_the_calls_report_the_caps_hold(ev):
    """Any sequence of calls, any reservation sizes, usage reported or not,
    each within its own reservation: the charged total never exceeds either
    cap, and nothing is flagged."""
    import random
    rng = random.Random(7)
    for _ in range(300):
        b = ev.Budget(rng.randint(5_000, 120_000), rng.randint(1_024, 40_000))
        for _ in range(rng.randint(1, 60)):
            reservation = (rng.randint(1, 30_000), rng.choice((1_024, 2_048, 4_096, 8_192)))
            if not b.reserve(*reservation):
                continue
            b.record_call(unreported() if rng.random() < 0.3
                          else usage(rng.randint(0, reservation[0]),
                                     rng.randint(0, reservation[1])), reservation)
            assert b.input <= b.max_input and b.output <= b.max_output
        assert not b.violated


def test_usage_above_its_reservation_is_a_violation_not_a_new_estimate(ev):
    """It used to raise an 'estimate' and carry on, overshooting the cap. A
    bound that is exceeded is a failure: counted, and the planner stops."""
    b = ev.Budget(50_000, 50_000)
    assert b.reserve(9_000, 4_096)
    b.record_call(usage(9_500, 100), (9_000, 4_096))
    assert b.violated and b.bound_violations == 1
    assert b.as_dict()["bound_violations"] == 1


# -- the planner honours the meter --------------------------------------------------

def planner_with(replies):
    from tests.unit.test_live_adapter_contract import make_planner
    return make_planner(replies)


def input_bound(request):
    from app.llm.token_bound import input_upper_bound
    return input_upper_bound(request)


def first_request(question, ctx=None):
    """The request the planner sends first for this question, captured from
    an unmetered run against the fake client."""
    from tests.unit.test_live_adapter_contract import FakeResponse, context_with, valid_plan_block
    planner = planner_with([FakeResponse([valid_plan_block()], None)])
    planner.plan(question, ctx or context_with(question))
    return planner._client.messages.requests[0]


def test_a_repair_the_budget_cannot_cover_is_never_made(ev):
    from app.llm.planner import PlannerBudgetExhausted
    from tests.unit.test_live_adapter_contract import (
        FakeBlock, FakeResponse, FakeUsage, context_with, valid_plan_block,
    )

    first = first_request("top accounts")
    # Room for the first call's bound and a little more: never for a repair,
    # whose request is the first one plus the correction.
    budget = ev.Budget(input_bound(first) + 1_000, 10_000)
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
# The four tests below reproduced it on the unmodified code
# (evidence/runs/r3-r3-reproduced.json).

def long_catalog_context(entries: int = 1_000):
    """A planning context whose prompt is far larger than any fixed estimate:
    the vocabulary a large customer's catalogue puts in every request."""
    from tests.unit.test_live_adapter_contract import context_with
    return context_with("top accounts", known_gpos=[
        f"Group Purchasing Organisation {i:04d}" for i in range(entries)])


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


def test_the_bound_covers_everything_the_request_carries():
    """System text (catalogue, conversation state), every message (question,
    repair), the tool schema and the tool choice all count; a longer
    catalogue or a repair makes a larger bound."""
    from app.llm.token_bound import FRAMING_ALLOWANCE, input_upper_bound

    small = first_request("top accounts")
    large = first_request("top accounts", long_catalog_context())
    assert input_upper_bound(large) - input_upper_bound(small) >= (
        len(large["system"].encode()) - len(small["system"].encode()))
    repair = dict(small, messages=small["messages"] + [
        {"role": "assistant", "content": "I produced an invalid plan."},
        {"role": "user", "content": "That plan failed validation: metric: Input should be ..."}])
    assert input_upper_bound(repair) > input_upper_bound(small)
    assert input_upper_bound(small) >= FRAMING_ALLOWANCE + len(small["system"].encode())


def test_text_that_normalisation_expands_is_measured_expanded():
    """One character can become many under NFKC; the larger length counts."""
    from app.llm.token_bound import input_upper_bound
    plain = {"max_tokens": 1, "system": "a" * 3, "messages": []}
    ligature = {"max_tokens": 1, "system": "\ufdfa", "messages": []}   # 3 bytes, NFKC 33
    assert input_upper_bound(ligature) - input_upper_bound(plain) >= 30


def test_a_repair_larger_than_what_is_left_is_refused_before_it_is_sent(ev):
    """The first call fits; the repair -- the same request plus the error --
    does not fit what the first call left. It is never sent."""
    from app.llm.planner import PlannerBudgetExhausted
    from tests.unit.test_live_adapter_contract import (
        FakeBlock, FakeResponse, FakeUsage, valid_plan_block,
    )
    ctx = long_catalog_context()
    first = first_request("top accounts", ctx)
    budget = ev.Budget(input_bound(first) + 500, 10_000)
    planner = planner_with([
        FakeResponse([FakeBlock(type="tool_use", name="emit_plan",
                                input={"metric": "not_a_metric"})], FakeUsage(9_000, 160)),
        FakeResponse([valid_plan_block()], FakeUsage(9_000, 160)),
    ])
    ctx = long_catalog_context()
    ctx.spend = budget
    with pytest.raises(PlannerBudgetExhausted, match="repair"):
        planner.plan("top accounts", ctx)
    assert len(planner._client.messages.requests) == 1
    assert budget.input == 9_000 <= budget.max_input


def test_the_record_says_how_the_bound_was_computed(ev):
    record = ev.Budget(1, 1).as_dict()
    assert "utf8" in record["input_bound_method"] and "checked" in record["input_bound_method"]
    assert record["output_bound_method"] == "the request's max_tokens"
