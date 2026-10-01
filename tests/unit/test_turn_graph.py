"""The turn graph's wiring, with an in-memory checkpointer and a fake turn.

What matters here is order and durability, not business logic, so the nodes
are fakes that record what ran. The properties:

* the clarification's side effect (record_question) runs exactly ONCE across
  an interrupt and a resume -- LangGraph re-runs an interrupted node from its
  top, which is why the side effect lives in its own node;
* await_reply is the node that re-runs, and does nothing before interrupting;
* a thread checkpointed part-way resumes from the next step, not the start;
* a runaway loop is stopped by the recursion limit.

An in-memory saver is used here only; everything else runs on PostgreSQL.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command, interrupt

from app.graph.turn import RECURSION_LIMIT, build_turn_graph


class FakeTurn:
    def __init__(self, ask=False, fail_answer_once=False):
        self.calls: list[str] = []
        self.ask = ask
        self.fail_answer_once = fail_answer_once

    def node_resolve(self, state):
        self.calls.append("resolve")
        if self.ask and not state.get("chosen"):
            return {"route": "ask", "asking": {"subject": "x", "choices": [{"id": "A"}, {"id": "B"}]}}
        return {"route": "plan"}

    def node_record_question(self, state):
        self.calls.append("record_question")
        return {"route": "wait"}

    def node_await_reply(self, state):
        self.calls.append("await_reply:start")
        reply = interrupt({"choices": state["asking"]["choices"]})
        self.calls.append("await_reply:resumed")
        return {"route": "resolve", "chosen": {"x": reply["id"]}, "asking": None}

    def node_plan(self, state):
        self.calls.append("plan")
        return {"route": "check", "plan": {"metric": "paid_pack_units"}}

    def node_check(self, state):
        self.calls.append("check")
        return {"route": "answer"}

    def node_answer(self, state):
        self.calls.append("answer")
        if self.fail_answer_once:
            self.fail_answer_once = False
            raise RuntimeError("crash after planning")
        return {"route": "end", "outcome": "answered"}


def config(thread="t"):
    return {"configurable": {"thread_id": thread}, "recursion_limit": RECURSION_LIMIT}


def test_a_plain_turn_runs_every_step_once_in_order():
    turn = FakeTurn()
    build_turn_graph(InMemorySaver()).invoke({}, config(), context=turn)
    assert turn.calls == ["resolve", "plan", "check", "answer"]


def test_the_clarification_is_recorded_once_across_interrupt_and_resume():
    graph = build_turn_graph(InMemorySaver())
    first = FakeTurn(ask=True)
    out = graph.invoke({}, config(), context=first)
    assert out.get("__interrupt__")
    assert first.calls == ["resolve", "record_question", "await_reply:start"]

    second = FakeTurn(ask=True)            # a different request resumes it
    graph.invoke(Command(resume={"id": "B"}), config(), context=second)
    assert "record_question" not in second.calls, "the clarify turn would be committed twice"
    assert second.calls[:2] == ["await_reply:start", "await_reply:resumed"]
    assert second.calls[2:] == ["resolve", "plan", "check", "answer"]


def test_a_thread_checkpointed_part_way_resumes_from_the_next_step():
    """A crash after planning must not plan again: planning is the step that
    costs money."""
    graph = build_turn_graph(InMemorySaver())
    first = FakeTurn(fail_answer_once=True)
    with pytest.raises(RuntimeError):
        graph.invoke({}, config(), context=first)
    assert first.calls == ["resolve", "plan", "check", "answer"]

    retry = FakeTurn()
    graph.invoke(None, config(), context=retry)
    assert retry.calls == ["answer"]


def test_a_loop_is_stopped_by_the_recursion_limit():
    class Loop(FakeTurn):
        def node_resolve(self, state):
            return {"route": "ask", "asking": {"subject": "x", "choices": [{"id": "A"}]}}

        def node_await_reply(self, state):
            return {"route": "resolve"}

    with pytest.raises(GraphRecursionError):
        build_turn_graph(InMemorySaver()).invoke({}, config(), context=Loop())


def test_the_context_is_never_checkpointed():
    """Identity arrives as runtime context on each invocation. Nothing about
    it may be written to workflow state."""
    saver = InMemorySaver()
    turn = FakeTurn(ask=True)
    turn.principal = "PRINCIPAL-SENTINEL-7f3a"
    build_turn_graph(saver).invoke({}, config(), context=turn)
    stored = repr(saver.storage) + repr(saver.blobs) + repr(saver.writes)
    assert "PRINCIPAL-SENTINEL-7f3a" not in stored
