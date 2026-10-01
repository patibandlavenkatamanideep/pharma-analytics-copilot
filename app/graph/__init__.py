"""Orchestration of a conversation turn as a durable LangGraph workflow."""

from __future__ import annotations

import os

from app.graph.turn import GRAPH_VERSION, RECURSION_LIMIT, TurnState, build_turn_graph

__all__ = ["GRAPH_VERSION", "RECURSION_LIMIT", "TurnState", "build_turn_graph",
           "checkpointer", "disable_external_tracing"]


def disable_external_tracing() -> None:
    """LangGraph's dependencies include the LangSmith client, which exports
    traces -- prompts, plans, questions -- to a hosted service whenever its
    environment switch is on. Nothing in this system is configured to use it
    and no external telemetry is permitted, so it is switched off unless an
    operator opts in explicitly with PAC_LANGSMITH_TRACING=true, and then only
    after the redaction the operator is responsible for configuring."""
    if os.environ.get("PAC_LANGSMITH_TRACING", "").lower() == "true":
        return
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        os.environ[name] = "false"


def checkpointer():
    """The PostgreSQL checkpointer, on the runtime role's app_graph pool.

    Follows the CURRENT pool rather than holding the one it was built with.
    Pools are closed and rebuilt -- close_pools() at shutdown, on a settings
    change, between test modules that switch databases -- and a saver bound
    to a closed pool fails every request after it, while the compiled graph
    that holds the saver lives for the whole process.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from app.db import graph_pool

    class _CurrentPoolSaver(PostgresSaver):
        @property
        def conn(self):
            return graph_pool()

        @conn.setter
        def conn(self, _value):
            pass

    return _CurrentPoolSaver(graph_pool())
