"""One conversation turn, as an explicit LangGraph workflow.

    START -> resolve -> plan -> check -> answer -> END
               |  ^
               |  +--------------------------+
               v                             |
          record_question -> await_reply ----+     (interrupt / resume)

Every node is a thin call into the request's context (``pipeline.Turn``);
the graph owns ordering, durability and resumption, not business logic. The
existing typed plan, compiler, validator, policy and renderer are unchanged
and run inside the nodes exactly as they did in the linear pipeline -- which
now IS this graph, so every existing test exercises it.

What is checkpointed (TurnState) is deliberately small: the question, the
choices a clarification offered, the typed plan, versions and counters.
What is NOT:

* **Identity.** The principal arrives as LangGraph runtime *context* on every
  invocation and is never part of state. A resumed thread is authorised by
  the request that resumes it, not by whoever started it.
* **Results.** An answer headline can carry revenue. Execution, rendering and
  the atomic commit happen inside one node, so rows and headlines never pass
  through a checkpoint.
* **Secrets.** Nothing here holds a token, a credential or a DSN.

Side effects sit at node boundaries. LangGraph re-runs an interrupted node
from its top on resume, so the clarification is two nodes: record_question
commits the clarify turn and completes (and is therefore never re-run), and
await_reply does nothing but interrupt.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

#: Bumped when TurnState's shape or meaning changes. A thread checkpointed
#: under another version is not resumed; it is restarted.
GRAPH_VERSION = "1.0.0"

#: Transitions per invocation. A turn is at most resolve, ask, wait, resolve,
#: plan, check, answer; anything longer is a loop, not a turn.
RECURSION_LIMIT = 16


class TurnState(TypedDict, total=False):
    graph_version: str
    dataset_id: str
    metric_version: str
    policy_version: str
    #: The question as first asked. A clarification's reply is a different
    #: string; the question being answered is still this one.
    question: str
    effective_question: str
    #: normalised phrase -> chosen entity id, from a clarification reply
    chosen: dict[str, str]
    #: the clarification this thread is waiting on: kind, subject, message,
    #: and the choices exactly as shown
    asking: dict[str, Any] | None
    named_accounts: list[list[str]]
    plan: dict[str, Any] | None
    planning: dict[str, Any] | None
    disclosures: list[str]
    model_calls: int
    route: str
    outcome: str


def _resolve(state: TurnState, runtime: Runtime[Any]) -> dict[str, Any]:
    return runtime.context.node_resolve(state)


def _record_question(state: TurnState, runtime: Runtime[Any]) -> dict[str, Any]:
    return runtime.context.node_record_question(state)


def _await_reply(state: TurnState, runtime: Runtime[Any]) -> dict[str, Any]:
    return runtime.context.node_await_reply(state)


def _plan(state: TurnState, runtime: Runtime[Any]) -> dict[str, Any]:
    return runtime.context.node_plan(state)


def _check(state: TurnState, runtime: Runtime[Any]) -> dict[str, Any]:
    return runtime.context.node_check(state)


def _answer(state: TurnState, runtime: Runtime[Any]) -> dict[str, Any]:
    return runtime.context.node_answer(state)


def _route(state: TurnState) -> str:
    return state.get("route") or "end"


def build_turn_graph(checkpointer):
    """The compiled graph. One per process; the checkpointer is shared and
    every request supplies its own context."""
    g = StateGraph(TurnState, context_schema=object)
    g.add_node("resolve", _resolve)
    g.add_node("record_question", _record_question)
    g.add_node("await_reply", _await_reply)
    g.add_node("plan", _plan)
    g.add_node("check", _check)
    g.add_node("answer", _answer)

    g.add_edge(START, "resolve")
    g.add_conditional_edges("resolve", _route,
                            {"ask": "record_question", "plan": "plan", "end": END})
    g.add_edge("record_question", "await_reply")
    g.add_conditional_edges("await_reply", _route, {"resolve": "resolve", "end": END})
    g.add_conditional_edges("plan", _route, {"check": "check", "end": END})
    g.add_conditional_edges("check", _route, {"answer": "answer", "end": END})
    g.add_edge("answer", END)
    return g.compile(checkpointer=checkpointer)
