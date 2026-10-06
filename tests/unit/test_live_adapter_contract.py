"""What the live adapter actually sends, and what it does with what comes back.

No network, no credentials, no inference. A fake transport stands in for
``client.messages.create`` and records the request, so these tests inspect the
real prompt and the real tool payload rather than a reimplementation of them.

They exist because the typed-cohort work was tested only through
``OfflinePlanner``. The prompt -- the only text a live model ever sees -- kept
describing every cohort as account ids and every turn with a predecessor as a
follow-up. The deterministic planner and the model that serves users therefore
did different things, and the suite could not tell.

These assert the CONTRACT: what is in the prompt, what the tool schema allows,
how a malformed response is handled. They assert nothing about how well a
model answers, which no fake transport can establish.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from app.analytics.plan import AnalyticalPlan
from app.conversation.continuity import Cohort, TurnKind
from app.conversation.continuity import resolve as resolve_continuity
from app.llm.planner import PlanningContext, build_system_prompt

ANCHOR = {"min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------

@dataclass
class FakeBlock:
    type: str
    name: str | None = None
    input: dict[str, Any] | None = None


@dataclass
class FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    usage: FakeUsage | None = None


class FakeMessages:
    """Records every request; replies from a scripted queue."""

    def __init__(self, replies: list[Any]):
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **request):
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("the adapter made more calls than were scripted")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)
        self.options: list[dict[str, Any]] = []

    def with_options(self, **options):
        """The per-attempt timeout and retry budget, recorded."""
        self.options.append(options)
        return self


def valid_plan_block(**overrides) -> FakeBlock:
    plan = {
        "metric": "paid_pack_units",
        "dimensions": ["account"],
        "time": {"kind": "named", "named": "r3m"},
        **overrides,
    }
    return FakeBlock(type="tool_use", name="emit_plan", input=plan)


def make_planner(replies):
    """A BedrockPlanner whose transport is fake. No client is constructed."""
    from app.llm.planner import BedrockPlanner

    planner = BedrockPlanner.__new__(BedrockPlanner)
    from app.config import get_settings
    planner.settings = get_settings()
    planner.model_id = "us.anthropic.fake-model-v1:0"
    planner.last_usage = {}
    planner._legacy_endpoint = True
    planner._client = FakeClient(replies)
    return planner


def context_with(question: str, **kw) -> PlanningContext:
    """Build a context the way the pipeline does -- continuity resolved once."""
    cohort = kw.pop("cohort", None)
    previous_plan = kw.pop("previous_plan", None)
    cont = resolve_continuity(question, previous_plan=previous_plan, cohort=cohort)
    return PlanningContext(
        role="exec", scope_description="all territories and regions",
        wac_authorized=True, reporting_anchor=ANCHOR,
        known_products=["ZENOVAX", "GEMTARA"],
        previous_plan=previous_plan,
        previous_cohort=list(cohort.ids) if cohort else [],
        previous_cohort_dimension=cohort.dimension if cohort else None,
        previous_cohort_complete=cohort.complete if cohort else True,
        continuity=cont, **kw)


# ---------------------------------------------------------------------------
# The prompt carries typed cohorts
# ---------------------------------------------------------------------------

def cohort_paragraph(prompt: str) -> str:
    return next(line for line in prompt.split("\n") if "previous answer's" in line)


def test_a_product_cohort_is_not_described_as_account_ids():
    """The reproduced defect. The prompt said "account ids" for every grain.

    Since 30 September the ids are not sent at all: the server binds the
    stored population to the query, whole, and the model is told only what
    it is and that it is frozen. The typing guarantee is unchanged -- and
    tested against the binding itself in test_cohort_typing.py.
    """
    cohort = Cohort(dimension="product", ids=("ZENOVAX", "GEMTARA"))
    prompt = build_system_prompt(context_with(
        "Show me those same ones by month",
        previous_plan={"metric": "paid_pack_units", "dimensions": ["product"]},
        cohort=cohort))
    paragraph = cohort_paragraph(prompt)

    assert "account ids" not in prompt
    assert "2 products" in paragraph
    assert "do NOT add product_names" in paragraph
    assert "ZENOVAX" not in paragraph, "ids are the server's to apply, not the model's"
    assert "account_ids" not in paragraph


def test_a_facility_cohort_names_the_facility_filter():
    cohort = Cohort(dimension="facility", ids=("ORG-1", "ORG-2"))
    prompt = build_system_prompt(context_with(
        "Break those down by month",
        previous_plan={"metric": "paid_pack_units", "dimensions": ["facility"]},
        cohort=cohort))
    paragraph = cohort_paragraph(prompt)
    assert "2 facilities" in paragraph and "facility_ids" in paragraph
    assert "account_ids" not in paragraph


def test_an_account_cohort_still_names_account_ids():
    """The fix must not break the grain that always worked."""
    cohort = Cohort(dimension="account", ids=("GP1", "GP2"))
    prompt = build_system_prompt(context_with(
        "Show me those same accounts by month",
        previous_plan={"metric": "paid_pack_units", "dimensions": ["account"]},
        cohort=cohort))
    paragraph = cohort_paragraph(prompt)
    assert "2 accounts" in paragraph and "account_ids" in paragraph
    assert "GP1" not in paragraph


def test_a_period_cohort_is_not_offered_as_a_population():
    """"Those same months" is a time window, not a set of entities."""
    cohort = Cohort(dimension="period_mo", ids=("2026-08", "2026-09"))
    prompt = build_system_prompt(context_with(
        "Show me those same ones by product",
        previous_plan={"metric": "paid_pack_units", "dimensions": ["period_mo"]},
        cohort=cohort))
    # The ids are never offered, and no freeze-this-cohort instruction is
    # emitted, because there is no population filter a period belongs in.
    assert "2026-08" not in prompt
    assert "the previous answer was about" not in prompt.lower()


def test_an_untyped_legacy_cohort_is_not_guessed_at():
    """Turns recorded before cohort_dimension existed have no grain."""
    cohort = Cohort(dimension="", ids=("SOMETHING", "ELSE"))
    prompt = build_system_prompt(context_with(
        "Show me those same ones by month",
        previous_plan={"metric": "paid_pack_units", "dimensions": []},
        cohort=cohort))
    assert "SOMETHING" not in prompt


# ---------------------------------------------------------------------------
# The prompt classifies the turn
# ---------------------------------------------------------------------------

def test_a_fresh_question_after_exclude_340b_is_told_it_is_fresh():
    """The regression the brief names. A self-contained question following
    'exclude 340B' must not inherit that filter."""
    previous = {"metric": "paid_pack_units", "dimensions": ["account"],
                "filters": {"is_340b": "exclude"}}
    prompt = build_system_prompt(context_with(
        "What is our total revenue this quarter?", previous_plan=previous))

    assert "This is a FRESH QUESTION" in prompt
    assert "This is a FOLLOW-UP" not in prompt
    assert "do NOT carry over its filters" in prompt


def test_an_explicit_modification_is_told_it_is_a_follow_up():
    previous = {"metric": "paid_pack_units", "dimensions": ["account"]}
    prompt = build_system_prompt(context_with(
        "Now break that down by quarter", previous_plan=previous))
    assert "This is a FOLLOW-UP" in prompt


def test_a_correction_is_labelled_as_a_correction():
    previous = {"metric": "paid_pack_units", "dimensions": ["account"]}
    prompt = build_system_prompt(context_with(
        "No, I meant by product", previous_plan=previous))
    assert "CORRECTS the previous question" in prompt


def test_a_first_turn_carries_no_continuation_language():
    prompt = build_system_prompt(context_with("What are our total pack units?"))
    assert "FOLLOW-UP" not in prompt
    assert "FRESH QUESTION" not in prompt


# ---------------------------------------------------------------------------
# An ambiguous reference is asked about, not guessed
# ---------------------------------------------------------------------------

def test_a_bare_those_is_ambiguous_and_asks():
    cont = resolve_continuity(
        "those?", previous_plan={"metric": "paid_pack_units"},
        cohort=Cohort(dimension="account", ids=("GP1",)))
    assert cont.kind is TurnKind.AMBIGUOUS_CONTINUATION
    assert cont.clarification
    assert not cont.carries_cohort


def test_a_truncated_cohort_is_disclosed_rather_than_frozen():
    """200 of 4,000 accounts is not "the previous result"."""
    cohort = Cohort(dimension="account", ids=tuple(f"GP{i}" for i in range(200)),
                    complete=False, total_available=4000)
    cont = resolve_continuity(
        "Show me those same accounts by month",
        previous_plan={"metric": "paid_pack_units"}, cohort=cohort)
    assert not cont.carries_cohort
    assert cont.clarification and "4,000" in cont.clarification


def test_a_complete_cohort_is_carried_and_disclosed():
    cohort = Cohort(dimension="account", ids=("GP1", "GP2"), complete=True)
    cont = resolve_continuity(
        "Show me those same accounts by month",
        previous_plan={"metric": "paid_pack_units"}, cohort=cohort)
    assert cont.carries_cohort
    assert any("2 accounts" in d for d in cont.disclosures)


# ---------------------------------------------------------------------------
# The tool payload
# ---------------------------------------------------------------------------

def test_the_tool_schema_forbids_unknown_fields_and_free_text_sql():
    planner = make_planner([FakeResponse([valid_plan_block()],
                                         FakeUsage(100, 20))])
    planner.plan("What are our top accounts?", context_with("What are our top accounts?"))

    request = planner._client.messages.requests[0]
    tool = request["tools"][0]
    assert tool["name"] == "emit_plan"
    assert request["tool_choice"] == {"type": "tool", "name": "emit_plan"}

    schema = json.dumps(tool["input_schema"]).lower()
    # The model cannot supply SQL, a table, a role or a scope: the plan type
    # has no field for any of them.
    for forbidden in ("\"sql\"", "\"query\"", "\"table\"", "\"role\"",
                      "\"scope\"", "\"user_id\"", "\"credential\""):
        assert forbidden not in schema, f"plan schema exposes {forbidden}"
    assert tool["input_schema"].get("additionalProperties") is False


def test_the_legacy_endpoint_is_not_sent_an_effort_field():
    """The InvokeModel path rejects unknown top-level fields."""
    planner = make_planner([FakeResponse([valid_plan_block()])])
    planner.plan("q", context_with("q"))
    assert "output_config" not in planner._client.messages.requests[0]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def test_a_valid_tool_call_becomes_a_planning_result():
    planner = make_planner([FakeResponse([valid_plan_block()], FakeUsage(1, 2))])
    result = planner.plan("q", context_with("q"))

    assert isinstance(result.plan, AnalyticalPlan)
    assert result.plan.metric.value == "paid_pack_units"
    # Identity travels with the result, so evidence can say which planner
    # and which prompt produced it.
    assert result.provider == "bedrock"
    assert result.model_id == "us.anthropic.fake-model-v1:0"
    assert result.prompt_version
    assert result.usage.input_tokens == 1 and result.usage.output_tokens == 2
    assert result.usage.known
    assert not result.repaired


def test_a_repair_keeps_the_first_attempts_tokens():
    """The tokens the failed attempt spent were still spent.

    last_usage was overwritten by whichever call finished last, so every
    repaired request under-reported its cost by the whole first attempt.
    """
    bad = FakeBlock(type="tool_use", name="emit_plan",
                    input={"metric": "not_a_real_metric",
                           "time": {"kind": "named", "named": "r3m"}})
    planner = make_planner([
        FakeResponse([bad], FakeUsage(100, 20)),
        FakeResponse([valid_plan_block()], FakeUsage(120, 25)),
    ])
    result = planner.plan("q", context_with("q"))

    assert result.plan.metric.value == "paid_pack_units"
    assert len(planner._client.messages.requests) == 2, "expected one repair"
    assert result.repaired
    assert [a.outcome for a in result.attempts] == ["invalid_plan", "plan"]
    # 100 + 120, not 120.
    assert result.usage.input_tokens == 220
    assert result.usage.output_tokens == 45


def test_repair_is_bounded_to_one_attempt():
    bad = FakeBlock(type="tool_use", name="emit_plan",
                    input={"metric": "nope", "time": {"kind": "named", "named": "r3m"}})
    planner = make_planner([FakeResponse([bad]), FakeResponse([bad])])
    from app.llm.planner import PlannerError

    with pytest.raises(PlannerError):
        planner.plan("q", context_with("q"))
    assert len(planner._client.messages.requests) == 2, "repair was not bounded"


def test_a_response_with_no_tool_call_is_an_error_not_a_guess():
    planner = make_planner([
        FakeResponse([FakeBlock(type="text")]),
        FakeResponse([FakeBlock(type="text")]),
    ])
    from app.llm.planner import PlannerError

    with pytest.raises(PlannerError):
        planner.plan("q", context_with("q"))


def test_a_transport_exception_propagates_as_a_planner_error():
    planner = make_planner([RuntimeError("connection reset")])
    with pytest.raises(Exception) as exc:
        planner.plan("q", context_with("q"))
    assert "provider_transport_error" in str(exc.value)
    assert "connection reset" not in str(exc.value)


def test_a_tool_call_under_another_name_is_not_accepted():
    """Only emit_plan is a plan. Anything else is not a tool this server owns."""
    planner = make_planner([
        FakeResponse([FakeBlock(type="tool_use", name="run_sql",
                                input={"sql": "SELECT 1"})]),
        FakeResponse([FakeBlock(type="tool_use", name="run_sql",
                                input={"sql": "SELECT 1"})]),
    ])
    from app.llm.planner import PlannerError

    with pytest.raises(PlannerError):
        planner.plan("q", context_with("q"))


# ---------------------------------------------------------------------------
# Usage accounting is request-local
# ---------------------------------------------------------------------------

def test_interleaved_requests_do_not_read_each_others_usage():
    """The defect this contract exists to remove.

    last_usage was instance state on a planner created once per process.
    Two requests in flight meant whichever finished last set the usage that
    both of them reported.
    """
    planner = make_planner([
        FakeResponse([valid_plan_block()], FakeUsage(10, 1)),
        FakeResponse([valid_plan_block()], FakeUsage(9000, 900)),
        FakeResponse([valid_plan_block()], FakeUsage(20, 2)),
    ])
    first = planner.plan("cheap one", context_with("cheap one"))
    second = planner.plan("expensive one", context_with("expensive one"))
    third = planner.plan("another cheap one", context_with("another cheap one"))

    assert first.usage.input_tokens == 10
    assert second.usage.input_tokens == 9000
    assert third.usage.input_tokens == 20
    # The results are immutable, so an earlier one cannot be rewritten by a
    # later call.
    assert first.usage.input_tokens == 10


def test_a_response_with_no_usage_is_unknown_not_zero():
    """"The provider told us nothing" and "it cost nothing" are different
    facts, and only one of them is safe to report."""
    planner = make_planner([FakeResponse([valid_plan_block()], usage=None)])
    result = planner.plan("q", context_with("q"))

    assert not result.usage.known
    assert result.usage.input_tokens is None
    assert result.usage.as_dict()["known"] is False


def test_a_transport_failure_ends_planning_without_a_false_repair():
    """A call that never came back is not an invalid plan. The repair used to
    tell the model "that plan failed validation" and call again -- false, and
    a second call to a provider that had just failed. The SDK has already
    retried what was retryable inside the request's budget; what reaches the
    planner is final, and the user is told the service is unavailable rather
    than asked to rephrase."""
    from app.llm.planner import PlannerUnavailable

    planner = make_planner([
        ConnectionError("reset by peer"),
        FakeResponse([valid_plan_block()], FakeUsage(50, 5)),
    ])
    with pytest.raises(PlannerUnavailable) as exc:
        planner.plan("q", context_with("q"))
    assert "provider_transport_error" in str(exc.value)
    assert "reset by peer" not in str(exc.value)
    assert len(planner._client.messages.requests) == 1


def test_a_failed_repair_reports_the_tokens_it_spent():
    bad = FakeBlock(type="tool_use", name="emit_plan",
                    input={"metric": "nope", "time": {"kind": "named", "named": "r3m"}})
    planner = make_planner([
        FakeResponse([bad], FakeUsage(100, 10)),
        FakeResponse([bad], FakeUsage(110, 11)),
    ])
    from app.llm.planner import PlannerError

    with pytest.raises(PlannerError) as exc:
        planner.plan("q", context_with("q"))
    # 210 tokens were spent and the operator should be able to see that.
    assert "210" in str(exc.value)


def test_partial_usage_is_preserved_rather_than_discarded():
    """A provider that reports input but not output still reported something."""
    from app.llm.planner import TokenUsage

    partial = TokenUsage(input_tokens=42, output_tokens=None)
    assert partial.known
    assert (partial + TokenUsage()).input_tokens == 42
    assert (TokenUsage() + partial).input_tokens == 42
    combined = partial + TokenUsage(input_tokens=8, output_tokens=3)
    assert combined.input_tokens == 50 and combined.output_tokens == 3


def test_the_offline_planner_reports_the_same_contract():
    """One shape for the pipeline and the evaluator to consume."""
    from app.llm.planner import OfflinePlanner, PlanningResult

    result = OfflinePlanner().plan("What are our total pack units?",
                                   context_with("What are our total pack units?"))
    assert isinstance(result, PlanningResult)
    assert result.provider == "offline"
    assert result.model_id is None
    # No provider was called, so zero tokens would be a measurement nobody
    # took.
    assert not result.usage.known


def test_a_planning_result_cannot_be_mutated_after_the_fact():
    planner = make_planner([FakeResponse([valid_plan_block()], FakeUsage(1, 1))])
    result = planner.plan("q", context_with("q"))
    with pytest.raises(Exception):
        result.provider = "something else"      # type: ignore[misc]


def test_failed_attempts_keep_reported_usage_without_provider_text():
    from app.llm.planner import PlannerError
    planner = make_planner([FakeResponse([FakeBlock(type='tool_use', name='emit_plan',
        input={'metric': 'MARKER_SECRET'})], FakeUsage(1234, 12))] * 2)
    with pytest.raises(PlannerError) as exc:
        planner.plan('q', context_with('q'))
    summary = exc.value.planning
    assert summary['usage'] == {'input_tokens': 2468, 'output_tokens': 24, 'known': True}
    assert summary['provider'] == 'bedrock'
    assert len(summary['attempts']) == 2
    assert summary['unknown_usage_calls'] == 0
    assert 'MARKER' not in str(summary)


def test_transport_failure_is_unknown_usage_not_no_call():
    from app.llm.planner import PlannerUnavailable
    planner = make_planner([ConnectionError('MARKER_SECRET')])
    with pytest.raises(PlannerUnavailable) as exc:
        planner.plan('q', context_with('q'))
    summary = exc.value.planning
    assert summary['calls'] == summary['unknown_usage_calls'] == 1
    assert not summary['usage']['known']
    assert 'MARKER' not in str(summary)
