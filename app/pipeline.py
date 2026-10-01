"""The request pipeline.

One question, one pass, in a fixed order. The ordering is the design: the plan
is authorized BEFORE it is compiled, the SQL is validated BEFORE it is executed,
and it executes on a connection whose privileges were decided before the model
ran. No step can be skipped by anything the model or the user says.

  1. resolve the principal (already done by the caller, from a verified session)
  2. open the user's conversation and load its structured state
  3. build the planning vocabulary, narrowed to the principal's scope
  4. plan  -- the model's only involvement
  5. clarify and stop, if the question is ambiguous or unsupported
  6. authorize the plan against the principal
  7. compile it from server-owned definitions
  8. validate the final SQL as an AST
  9. execute in a read-only, time-bounded, scope-bound transaction
 10. render numbers from the result and attach data-quality findings
 11. persist the structured turn and write an audit row
"""

from __future__ import annotations

import logging
import time
import uuid

import psycopg
from dataclasses import dataclass, field
from typing import Any

from app import telemetry
from app.analytics.compiler import (
    CohortBinding, Compiler, CompileError, UnsupportedCombination)
from app.analytics.entities import vocabulary_for
from app.analytics.intent import blocking, find_gaps, resolve_mentions
from app.analytics.mentions import entity_index, normalise
from app.conversation.continuity import Cohort, summarise_cohort
from app.conversation.continuity import resolve as resolve_continuity
from app.analytics.periods import PeriodError
from app.analytics.registry import get_registry
from app.analytics.render import Answer, GrainError, render
from app.analytics.validator import SqlValidationError, validate
from app.auth.policy import AuthorizationError, Principal, authorize, scope_note
from app.config import get_settings
from app.conversation import runs
from app.conversation.clarify import choice_from_reply
from app.conversation.state import (
    Finalised, StagedTurn, finalise, open_conversation)
from app.db import (
    GenerationChanged, ScopeBindingError, analytics_transaction, auth_transaction)
from app.llm.planner import (
    Planner, PlannerBudgetExhausted, PlannerError, PlannerOutOfTime, PlannerUnavailable,
    typed_plan,
)
from app.analytics.plan import AnalyticalPlan
from app.graph import (
    GRAPH_VERSION, RECURSION_LIMIT, build_turn_graph, checkpointer,
    disable_external_tracing)
from langgraph.errors import GraphRecursionError
from langgraph.types import Command, interrupt

log = logging.getLogger(__name__)

POLICY_VERSION = "1.0.0"


#: Every field the audit row carries. The pipeline may set only these keys
#: (or a key in AUDIT_TRANSIENT); tests/security/test_audit_contract.py
#: checks both that every set key is here and that every name here is a
#: column, because a missing column fails the whole INSERT -- and the write
#: is best-effort, so the entire row would vanish without a trace.
AUDIT_COLUMNS = (
    "request_id", "user_id", "role", "scope_kind", "scope_value", "wac_authorized",
    "dataset_id", "metric_version", "policy_version", "plan_hash", "sql_hash",
    "status", "denial_reason", "row_count", "db_ms", "total_ms", "model_id",
    "input_tokens", "output_tokens",
    "reason_codes", "intent_gaps", "turn_kind", "prompt_version",
    "planner_attempts", "planner_repaired", "usage_known",
)

#: Keys used while building the row and deliberately not stored.
AUDIT_TRANSIENT = frozenset({"blocking_gaps"})


@dataclass
class PipelineResult:
    status: str                      # answered | clarify | denied | error
    conversation_id: str
    message: str
    answer: Answer | None = None
    alternative: str | None = None
    interpretation: str | None = None
    plan: dict[str, Any] | None = None
    sql: str | None = None           # returned only when explicitly requested
    timings: dict[str, int] = field(default_factory=dict)
    request_id: str = ""
    #: What THIS request's planning cost and which planner produced it.
    #: Carried on the result rather than read back off the planner instance,
    #: which under concurrency belonged to whichever call finished last.
    planning: dict[str, Any] | None = None
    #: For a clarification between real options -- an account name shared by
    #: several accounts -- the options, as {id, label, detail}. The user picks
    #: one instead of guessing at a spelling that would disambiguate.
    choices: list[dict[str, str]] = field(default_factory=list)
    #: Whether this turn is part of the conversation now:
    #:   saved      -- committed, with its run outcome, atomically
    #:   not_saved  -- nothing to record (a failure before any outcome)
    #:   failed     -- an outcome existed and could not be recorded
    #:   conflict   -- the conversation moved on; this was not recorded
    #: Returned to the client, so the interface never implies a turn was
    #: saved when it was not.
    persistence: str = "not_saved"
    run_id: str = ""
    #: The response body, built once here so the copy stored for replay is
    #: exactly what the client received.
    payload: dict[str, Any] | None = None
    #: The population the server froze into this answer, if any: (dimension,
    #: ids). Kept on the result for evaluation and audit; not sent to the
    #: client, which already has the previous answer's rows.
    applied_cohort: tuple[str, tuple[str, ...]] | None = None


def to_payload(result: PipelineResult, include_sql: bool) -> dict[str, Any]:
    """The client-facing body. Also what is stored for idempotent replay."""
    payload: dict[str, Any] = {
        "status": result.status,
        "conversation_id": result.conversation_id,
        "message": result.message,
        "request_id": result.request_id,
        "run_id": result.run_id,
        "persistence": result.persistence,
    }
    if result.alternative:
        payload["alternative"] = result.alternative
    if result.choices:
        # Options the caller can already see: they came from the caller's own
        # scoped index, so listing them discloses nothing new.
        payload["choices"] = result.choices
    if include_sql:
        # The typed plan is returned alongside the SQL, under the same explicit
        # request. It is strictly less sensitive than the SQL -- it names a
        # metric key, dimensions and filter values, and by construction cannot
        # contain a role, a scope, a table or a column -- and it is the thing
        # actually worth inspecting, because it is what the model produced and
        # what everything downstream was compiled from.
        payload["plan"] = result.plan
    if result.sql:
        payload["sql"] = result.sql
    if result.answer:
        a = result.answer
        payload["answer"] = {
            "headline": a.headline,
            "columns": a.columns,
            "rows": a.table,
            "scope_note": a.scope_note,
            "period_note": a.period_note,
            "warnings": a.warnings,
            "notes": a.notes,
            "row_count": a.row_count,
            "truncated": a.truncated,
        }
    return payload


class StaleState(RuntimeError):
    """A checkpoint written under a graph version this process does not run."""


class DeadlineExceeded(RuntimeError):
    """The request's wall-clock budget ran out between steps."""


class Pipeline:
    #: A metered spend for model calls (PlanningContext.spend). Set only by
    #: evaluation tooling; serving requests are bounded by per-user quotas.
    spend: Any = None

    def __init__(self, planner: Planner) -> None:
        self.planner = planner
        self.settings = get_settings()
        self.compiler = Compiler(max_rows=self.settings.max_result_rows)
        self._graph = None
        disable_external_tracing()

    @property
    def graph(self):
        """Built on first use, so constructing a Pipeline needs no database."""
        if self._graph is None:
            self._graph = build_turn_graph(checkpointer())
        return self._graph

    # -- dataset manifest ----------------------------------------------------

    def current_dataset(self) -> dict[str, Any]:
        """The published snapshot. A partially loaded refresh is never visible.

        Read over the auth connection, not the owner one. This runs on every
        ask(), and the owner role can create and drop objects and owns the
        protected tables -- there is no reason for the serving process to use
        it to read one manifest row. The auth role holds exactly SELECT on
        app_meta.dataset_manifest.
        """
        with auth_transaction() as cur:
            cur.execute(
                "SELECT dataset_id, load_mode, reporting_anchor, row_counts, warnings, "
                "       published_at, source_coverage, parent_dataset_id, "
                "       (SELECT max(last_batch_at) FROM app_ingest.watermarks) "
                "         AS last_ingest_at "
                "FROM app_meta.dataset_manifest WHERE load_state = 'published' "
                "ORDER BY published_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("no published dataset -- run scripts/load_data.py")
        return dict(row)

    # -- main entry point ----------------------------------------------------

    #: Refusals that are outcomes, not failures, and the status they are
    #: counted under.
    _REFUSALS = {"RunBusy": "busy", "IdempotencyConflict": "idempotency_conflict",
                 "ReplayUnavailable": "access_changed", "QuotaExceeded": "rate_limited"}

    def ask(
        self,
        principal: Principal,
        question: str,
        *,
        conversation_id: str | None = None,
        include_sql: bool = False,
        idempotency_key: str | None = None,
    ) -> PipelineResult:
        """Answer one question, inside the request's span."""
        started = time.perf_counter()
        status, persistence = "error", "not_saved"
        refusals = (runs.RunBusy, runs.IdempotencyConflict, runs.ReplayUnavailable,
                    runs.QuotaExceeded)
        with telemetry.span("pac.ask", expected=refusals, **{
                "pac.role": principal.role, "pac.release": self.settings.release,
                "pac.registry_version": get_registry().version,
                "pac.policy_version": POLICY_VERSION,
                "pac.graph_version": GRAPH_VERSION}) as span:
            try:
                result = self._ask(principal, question, conversation_id=conversation_id,
                                   include_sql=include_sql, idempotency_key=idempotency_key)
                status, persistence = result.status, result.persistence
                span.set(**{"pac.status": result.status, "pac.persistence": result.persistence,
                            "pac.request_id": result.request_id, "pac.run_id": result.run_id,
                            "pac.replayed": bool((result.payload or {}).get("replayed"))})
                return result
            except refusals as exc:
                status = self._REFUSALS[type(exc).__name__]
                raise
            finally:
                telemetry.count("pac.ask.outcomes", status=status, role=principal.role,
                                persistence=persistence)
                telemetry.observe("pac.ask.duration",
                                  (time.perf_counter() - started) * 1000, status=status)

    def _ask(
        self,
        principal: Principal,
        question: str,
        *,
        conversation_id: str | None = None,
        include_sql: bool = False,
        idempotency_key: str | None = None,
    ) -> PipelineResult:
        """Outside the graph: everything that must not be repeated if a node
        is replayed -- identifying the request, opening the conversation,
        taking the run's lease. Inside it: everything else."""
        dataset = self.current_dataset()
        telemetry.annotate(**{"pac.dataset_id": dataset["dataset_id"]})
        request_hash = runs.payload_hash(question, conversation_id, include_sql)

        # A retried request is recognised BEFORE a conversation is opened:
        # the first turn of a new conversation arrives with no conversation
        # id, and opening one first would start a second conversation for
        # the same request. The key is scoped to the user; the conversation
        # is part of the request hash.
        if idempotency_key:
            prior = runs.find(principal, idempotency_key)
            if prior is not None:
                if prior["payload_hash"] != request_hash:
                    raise runs.IdempotencyConflict(
                        "That request key was already used for a different question.")
                conversation_id = prior["conversation_id"]

        # Continuation is bound to the dataset and the semantic contracts, not
        # only to who is asking: a refresh or a contract bump makes a carried
        # plan incomparable rather than merely old.
        with telemetry.span("pac.state_load"):
            state = open_conversation(
                principal, conversation_id,
                dataset_id=dataset["dataset_id"],
                metric_version=get_registry().version,
                policy_version=POLICY_VERSION,
            )

            # One live run per conversation, and one committed outcome per key.
            # Raises RunBusy / IdempotencyConflict / ReplayUnavailable, which
            # the API maps to 409 or 403.
            run = runs.acquire(
                principal, state.conversation_id,
                revision=state.revision, request_hash=request_hash,
                idempotency_key=idempotency_key,
                lease_seconds=self.settings.run_lease_seconds,
                retention_seconds=self.settings.idempotency_retention_seconds,
                limits=(self.settings.user_requests_per_minute,
                        self.settings.user_requests_per_hour,
                        self.settings.user_concurrent_runs),
            )
        if run.replay is not None:
            replayed = dict(run.replay)
            return PipelineResult(
                status=replayed.get("status", "answered"),
                conversation_id=replayed.get("conversation_id", state.conversation_id),
                message=replayed.get("message", ""),
                request_id=replayed.get("request_id", ""),
                payload={**replayed, "replayed": True},
                persistence="saved", run_id=run.run_id,
            )

        turn = Turn(self, principal, question, include_sql, dataset, state, run)
        # Graph nodes parent their spans on this, whichever thread runs them.
        turn.otel_parent = telemetry.current()
        thread_id, graph_input = self._entry(turn)
        turn.thread_id = thread_id
        config = {"configurable": {"thread_id": thread_id},
                  "recursion_limit": RECURSION_LIMIT}
        # durability="sync": each step's checkpoint is written before the next
        # step starts, so a crash loses at most the step in flight. The
        # default writes in the background and can lose a completed step too.
        # Measured on a plain answered turn (offline planner, full dataset,
        # 20 requests): 55.3 ms median, against 52.8 ms with background writes
        # and 53.2 ms for the linear pipeline before the graph.
        try:
            try:
                out = self.graph.invoke(graph_input, config, context=turn, durability="sync")
            except StaleState:
                # Written by a different graph version: not resumed, restarted.
                self._forget(thread_id)
                turn.thread_id = thread_id = self._fresh_thread(turn)
                config["configurable"]["thread_id"] = thread_id
                out = self.graph.invoke(turn.initial_state(), config, context=turn,
                                        durability="sync")
        except runs.Cancelled:
            if turn.result is None:
                turn.finish(PipelineResult(
                    status="cancelled", conversation_id=state.conversation_id,
                    message="Cancelled."), "cancelled")
                runs.fail(run, None, status="cancelled")
            out = {}
        except DeadlineExceeded:
            if turn.result is None:
                turn.finish(PipelineResult(
                    status="error", conversation_id=state.conversation_id,
                    message=("That question took too long to answer. Narrowing it "
                             "usually helps -- a shorter period, one product, fewer "
                             "groupings.")),
                    "deadline_exceeded")
            out = {}
        except GraphRecursionError:
            log.error("turn graph exceeded %s transitions (run %s)", RECURSION_LIMIT, run.run_id)
            if turn.result is None:
                turn.finish(PipelineResult(
                    status="error", conversation_id=state.conversation_id,
                    message="That question could not be completed. Please try again."),
                    "graph_recursion")
            out = {}
        except Exception:
            # Unexpected. The run is closed so its key can retry -- and the
            # thread is KEPT, so a retry resumes from the last completed step
            # rather than planning again.
            if turn.result is None:
                runs.fail(run, None)
            raise

        waiting = bool((out or {}).get("__interrupt__"))
        if not waiting:
            # A finished thread is pruned at once: its outcome is in the run
            # and the turn, and a checkpoint is workflow state, not history.
            self._forget(thread_id)
        pending = state.pending_clarification
        if pending and pending.graph_thread_id and pending.graph_thread_id != thread_id:
            # A different question superseded the pending one; its paused
            # thread can never be resumed now.
            self._forget(pending.graph_thread_id)
        return turn.result

    # -- threads ---------------------------------------------------------------

    def _entry(self, turn: "Turn") -> tuple[str, Any]:
        """Which thread this request runs on, and with what input.

        * A reply that picks one of a pending clarification's choices resumes
          the thread that asked -- the choice is re-validated inside the graph
          against THIS request's access.
        * A retry of a run that failed mid-flight resumes that run's thread
          from its last completed step.
        * Anything else starts a new thread.
        """
        state = turn.state
        pending = state.pending_clarification
        if pending is not None:
            picked = choice_from_reply(turn.question, pending.choices)
            if picked is not None:
                option = pending.choices[picked]
                turn.resolving = pending.clarification_id
                if pending.graph_thread_id and self._waiting(pending.graph_thread_id):
                    return pending.graph_thread_id, Command(resume={"id": option["id"]})
                # No paused thread to resume (pruned, or asked before the
                # graph existed): run the original question with the choice
                # bound. resolve re-validates it either way.
                return self._fresh_thread(turn), turn.initial_state(
                    question=pending.question,
                    chosen={normalise(pending.slot_text or ""): option["id"]})

        own = self._thread_for(turn.run.run_id, state.conversation_id)
        if self._resumable(own):
            return own, None
        return self._fresh_thread(turn), turn.initial_state()

    @staticmethod
    def _thread_for(run_id: str, conversation_id: str) -> str:
        return f"{conversation_id}.{run_id}"

    def _fresh_thread(self, turn: "Turn") -> str:
        return self._thread_for(turn.run.run_id, turn.state.conversation_id)

    def _snapshot(self, thread_id: str):
        return self.graph.get_state({"configurable": {"thread_id": thread_id}})

    def _waiting(self, thread_id: str) -> bool:
        snap = self._snapshot(thread_id)
        return bool(snap.next) and any(t.interrupts for t in snap.tasks)

    def _resumable(self, thread_id: str) -> bool:
        """Checkpointed part-way through by a run that died, not paused."""
        snap = self._snapshot(thread_id)
        return bool(snap.next) and not any(t.interrupts for t in snap.tasks)

    def _forget(self, thread_id: str) -> None:
        try:
            self.graph.checkpointer.delete_thread(thread_id)
        except Exception:                                          # pragma: no cover
            log.warning("could not prune checkpoint thread %s", thread_id, exc_info=True)

    # -- audit -----------------------------------------------------------------

    def _write_audit(self, audit: dict[str, Any]) -> None:
        """Hashes, counts, codes and timings only -- never WAC values, result
        rows, prompts or text from the question."""
        # Derived before the write, so a code is present on every outcome
        # path rather than only where someone remembered to add it.
        codes = [audit.get("status")] + list(audit.get("blocking_gaps") or [])
        audit["reason_codes"] = [c for c in dict.fromkeys(codes) if c]

        unknown = set(audit) - set(AUDIT_COLUMNS) - AUDIT_TRANSIENT
        if unknown:
            # A key that is set and never persisted is a field that silently
            # never reaches the audit trail. Six did, until 30 September.
            log.error("audit keys with no column, dropped: %s", sorted(unknown))
        columns = list(AUDIT_COLUMNS)
        values = [audit.get(c) for c in columns]
        placeholders = ", ".join(["%s"] * len(columns))
        try:
            with telemetry.span("pac.audit"), auth_transaction() as cur:
                # Keyed by request: a node replayed after a crash cannot
                # record the same request twice.
                cur.execute(
                    f"INSERT INTO app_meta.query_audit ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) ON CONFLICT (request_id) DO NOTHING",
                    values,
                )
        except Exception:
            log.warning("failed to write audit row", exc_info=True)
            telemetry.count("pac.persistence.failures", kind="audit")


class Turn:
    """One request's context: who is asking, under which run, against which
    snapshot. Passed to the graph as runtime context -- never checkpointed --
    so identity and results stay out of workflow state.

    The node_* methods are the pipeline's stages, unchanged in substance from
    the linear version; the graph decides their order and makes them durable.
    """

    def __init__(self, pipe: Pipeline, principal: Principal, question: str,
                 include_sql: bool, dataset: dict[str, Any], state, run) -> None:
        self.pipe = pipe
        self.principal = principal
        self.question = question
        self.include_sql = include_sql
        self.dataset = dataset
        self.anchor = dataset["reporting_anchor"]
        self.state = state
        self.run = run
        self.thread_id = ""
        self.otel_parent = None
        self.request_id = uuid.uuid4().hex[:16]
        self.started = time.perf_counter()
        self.deadline_at = time.time() + pipe.settings.request_deadline_seconds
        self.timings: dict[str, int] = {}
        self.staged: StagedTurn | None = None
        self.resolving: str | None = None
        self.planning_summary: dict[str, Any] | None = None
        self.result: PipelineResult | None = None
        self.applied: tuple[str, tuple[str, ...]] | None = None
        self.audit: dict[str, Any] = {
            "request_id": self.request_id,
            "user_id": principal.user_id,
            "role": principal.role,
            "scope_kind": principal.scope_kind,
            "scope_value": principal.scope_value,
            "wac_authorized": principal.wac_authorized,
            "dataset_id": dataset["dataset_id"],
            "metric_version": get_registry().version,
            "policy_version": POLICY_VERSION,
        }
        self._vocab = None
        self._index = None
        self._continuity: dict[str, Any] = {}

    # -- shared, per request ---------------------------------------------------

    @property
    def vocab(self):
        if self._vocab is None:
            self._vocab = vocabulary_for(self.principal, self.dataset["dataset_id"])
        return self._vocab

    @property
    def index(self):
        if self._index is None:
            self._index = entity_index(self.principal, self.dataset["dataset_id"], self.vocab)
        return self._index

    def continuity(self, question: str):
        """One decision about what this turn is, shared by every planner and
        recomputed from the conversation as it is NOW -- never read back from
        a checkpoint, which could predate another committed turn."""
        if question not in self._continuity:
            state = self.state
            cohort = None
            if state.previous_cohort:
                cohort = Cohort(
                    dimension=state.previous_cohort_dimension or "",
                    ids=tuple(state.previous_cohort),
                    dataset_id=self.dataset["dataset_id"],
                    complete=state.previous_cohort_complete,
                    total_available=state.previous_cohort_total,
                )
            # Typed fields only: a model's free text from an earlier turn is
            # not context for this one (see planner.typed_plan).
            self._continuity[question] = resolve_continuity(
                question, previous_plan=typed_plan(state.previous_plan), cohort=cohort)
        return self._continuity[question]

    def initial_state(self, *, question: str | None = None,
                      chosen: dict[str, str] | None = None) -> dict[str, Any]:
        q = question or self.question
        return {
            "graph_version": GRAPH_VERSION,
            "dataset_id": self.dataset["dataset_id"],
            "metric_version": get_registry().version,
            "policy_version": POLICY_VERSION,
            "question": q,
            "effective_question": q,
            "chosen": dict(chosen or {}),
            "asking": None,
            "named_accounts": [],
            "plan": None,
            "planning": None,
            "disclosures": [],
            "model_calls": 0,
        }

    def guard(self, state: dict[str, Any]) -> None:
        """Every node starts here. A checkpoint from another graph version is
        restarted rather than resumed, and a request past its budget stops
        between steps rather than starting a new one."""
        if state.get("graph_version") != GRAPH_VERSION:
            raise StaleState(state.get("graph_version"))
        if time.time() > self.deadline_at:
            raise DeadlineExceeded()
        # Cooperative: checked between steps. A statement already running is
        # bounded by its timeout rather than interrupted mid-scan.
        if runs.cancel_requested(self.run.run_id):
            raise runs.Cancelled()

    # -- recording -------------------------------------------------------------

    def stage(self, plan: dict[str, Any] | None, answer_text: str, status: str,
              **extra: Any) -> None:
        self.staged = StagedTurn(
            question=self.question, plan=plan, answer_text=answer_text,
            status=status, resolves_clarification=self.resolving, **extra)

    def finish(self, result: PipelineResult, status: str, **extra: Any) -> PipelineResult:
        timings = self.timings
        timings["total_ms"] = int((time.perf_counter() - self.started) * 1000)
        result.timings = timings
        result.request_id = self.request_id
        result.run_id = self.run.run_id
        if result.planning is None:
            result.planning = self.planning_summary
        if self.applied and result.applied_cohort is None:
            result.applied_cohort = self.applied

        turn = self.staged
        if turn is None:
            # Nothing to record -- a failure before an outcome existed. The
            # run is closed so its key can be retried.
            result.persistence = "not_saved"
            result.payload = to_payload(result, self.include_sql)
            runs.fail(self.run, None)
        else:
            result.persistence = "saved"
            result.payload = to_payload(result, self.include_sql)
            try:
                with telemetry.span("pac.finalise", parent=self.otel_parent):
                    done = finalise(self.principal, self.state, self.run, turn,
                                    result.payload)
            except Exception:
                # Stated, not swallowed: the answer is returned, and the
                # response says it was not saved, so nothing implies the next
                # turn can build on it.
                log.exception("failed to commit turn for run %s", self.run.run_id)
                runs.fail(self.run, None)
                done = Finalised(persisted=False, reason="error")
                telemetry.count("pac.persistence.failures", kind="turn")
            if done.conflict:
                # The conversation moved while this was being answered: a
                # lease expired and another turn committed. The answer was
                # planned against state that is no longer current, so it is
                # withheld rather than shown as a continuation.
                status = "conflict"
                result = PipelineResult(
                    status="conflict", conversation_id=self.state.conversation_id,
                    message=("This conversation moved on while that was being "
                             "answered. Ask again to continue from the latest turn."),
                    request_id=self.request_id, run_id=self.run.run_id,
                    timings=timings, persistence="conflict")
                result.payload = to_payload(result, self.include_sql)
            elif not done.persisted:
                result.persistence = "failed"
                result.payload = to_payload(result, self.include_sql)
        self.audit.update(status=status, total_ms=timings["total_ms"], **extra)
        self.pipe._write_audit(self.audit)
        self.result = result
        return result

    def _clarify(self, message: str, *, reason: str | None = None,
                 plan: dict[str, Any] | None = None, stage_plan: bool = False,
                 **extra: Any) -> dict[str, Any]:
        self.stage(plan if stage_plan else None, message, "clarify", **extra)
        self.finish(PipelineResult(status="clarify",
                                   conversation_id=self.state.conversation_id,
                                   message=message, plan=plan,
                                   choices=extra.get("clarification", {}) and
                                   extra["clarification"]["choices"] or []),
                    "clarify", **({"denial_reason": reason} if reason else {}))
        return {"route": "end", "outcome": "clarify"}

    # -- nodes -----------------------------------------------------------------

    def node_resolve(self, state: dict[str, Any]) -> dict[str, Any]:
        """Continuity, then the entities the question names."""
        self.guard(state)
        question = state["effective_question"]
        chosen = state.get("chosen") or {}

        # A stored option is not a grant. The choice came from a clarification
        # shown earlier -- possibly to a different request, possibly before
        # access changed -- so it must still be in THIS caller's index.
        for entity_id in chosen.values():
            if entity_id not in self.index.account_ids:
                return self._clarify(
                    "That option is no longer available to you. Ask the question "
                    "again to see current options.",
                    reason="clarification_choice_unavailable")

        continuity = self.continuity(question)
        self.audit["turn_kind"] = continuity.kind.value
        if continuity.clarification:
            # An ambiguous reference is asked about, not guessed at.
            return self._clarify(continuity.clarification,
                                 reason=f"ambiguous:{continuity.kind.value}")

        # Resolved on the server, under the caller's scope, BEFORE planning.
        # An unknown or ambiguous name has no plan that could fix it, so it is
        # asked about without spending a model call; and the ids of accounts
        # the question names -- those ids and no others -- go to the planner,
        # which never sees the account catalog.
        mentions, unresolved = resolve_mentions(question, self.vocab, self.index,
                                                resolved=chosen)
        if unresolved:
            self.audit["intent_gaps"] = [g.kind for g in unresolved]
            self.audit["blocking_gaps"] = [g.kind for g in unresolved]
            message = " ".join(g.message() for g in unresolved)
            reason = ",".join(sorted({g.kind for g in unresolved}))
            # One question at a time: the first ambiguous name is stored with
            # the choices exactly as shown, so "the second one" means what the
            # user saw second. A later ambiguity is asked about on the re-run.
            asking = next((g for g in unresolved if g.choices), None)
            if asking is None:
                return self._clarify(message, reason=reason)
            return {"route": "ask", "asking": {
                "kind": asking.kind, "subject": asking.subject,
                "message": message, "reason": reason,
                "choices": [{"id": c[0], "label": c[1], "detail": c[2]}
                            for c in asking.choices],
            }}
        return {"route": "plan", "named_accounts": [
            [m.text, m.ids[0]] for m in mentions
            if m.kind == "account" and len(m.ids) == 1 and not m.reference_only]}

    def node_record_question(self, state: dict[str, Any]) -> dict[str, Any]:
        """Commit the clarify turn. Its own node, so it completes -- and is
        checkpointed -- before the interrupt: LangGraph re-runs an
        interrupted node from its top, and this must happen exactly once."""
        self.guard(state)
        asking = state["asking"]
        self._clarify(asking["message"], reason=asking["reason"], clarification={
            "kind": asking["kind"], "question": state["question"],
            "slot_text": asking["subject"], "choices": asking["choices"],
            "graph_thread_id": self.thread_id,
        })
        return {"route": "wait"}

    def node_await_reply(self, state: dict[str, Any]) -> dict[str, Any]:
        """Pause until the user chooses. Nothing before the interrupt, because
        everything before it would run again on resume."""
        reply = interrupt({"choices": state["asking"]["choices"]})
        subject = normalise(state["asking"]["subject"] or "")
        return {"route": "resolve", "asking": None,
                "chosen": {**(state.get("chosen") or {}), subject: reply["id"]}}

    def node_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        from app.llm.planner import PlanningContext

        self.guard(state)
        question = state["effective_question"]
        vocab, principal, state_now = self.vocab, self.principal, self.state
        context = PlanningContext(
            role=principal.role,
            scope_description=principal.scope_description,
            wac_authorized=principal.wac_authorized,
            reporting_anchor=self.anchor,
            known_products=vocab.products,
            known_subcategories=vocab.subcategories,
            known_categories=vocab.categories,
            known_specialties=vocab.specialties,
            known_gpos=vocab.gpos,
            known_archetypes=vocab.archetypes,
            known_territories=vocab.territories,
            known_regions=vocab.regions,
            all_territories=vocab.all_territories,
            all_regions=vocab.all_regions,
            previous_plan=typed_plan(state_now.previous_plan),
            previous_cohort=state_now.previous_cohort,
            previous_cohort_dimension=state_now.previous_cohort_dimension,
            previous_cohort_complete=state_now.previous_cohort_complete,
            previous_cohort_total=state_now.previous_cohort_total,
            continuity=self.continuity(question),
            named_accounts=[tuple(x) for x in state.get("named_accounts") or []],
            deadline_at=self.deadline_at,
            spend=self.pipe.spend,
        )
        t0 = time.perf_counter()
        try:
            planning = self.pipe.planner.plan(question, context)
        except PlannerOutOfTime:
            # The same outcome as a deadline met between steps.
            raise DeadlineExceeded() from None
        except PlannerBudgetExhausted as exc:
            self.finish(PipelineResult(
                status="error", conversation_id=self.state.conversation_id,
                message="Not run: the evaluation's spend limit is exhausted.",
            ), "budget_exhausted", denial_reason=str(exc)[:200])
            return {"route": "end", "outcome": "error"}
        except PlannerUnavailable as exc:
            log.warning("planner unavailable: %s", exc)
            self.finish(PipelineResult(
                status="error", conversation_id=self.state.conversation_id,
                message=("The question service is busy or unreachable right now. "
                         "Please try again in a moment."),
            ), "planner_unavailable", denial_reason=str(exc)[:200])
            return {"route": "end", "outcome": "error"}
        except PlannerError as exc:
            log.warning("planner failed: %s", exc)
            self.finish(PipelineResult(
                status="error", conversation_id=self.state.conversation_id,
                message=(
                    "I could not interpret that question. Try naming the metric, the "
                    "product or account, and the time period — for example "
                    "'top 10 accounts by pack units last quarter'."
                ),
            ), "planner_error", denial_reason=str(exc)[:200])
            return {"route": "end", "outcome": "error"}
        self.timings["plan_ms"] = int((time.perf_counter() - t0) * 1000)

        usage = planning.usage.as_dict()
        telemetry.annotate(**{
            "pac.provider": planning.provider, "pac.model_id": planning.model_id,
            "pac.prompt_version": planning.prompt_version,
            "pac.planner_version": planning.planner_contract_version,
            "pac.attempts": len(planning.attempts), "pac.repaired": planning.repaired,
            "pac.tokens.input": usage.get("input_tokens"),
            "pac.tokens.output": usage.get("output_tokens"),
            "pac.usage_known": usage.get("known")})
        summary = {
            "provider": planning.provider,
            "model_id": planning.model_id,
            "prompt_version": planning.prompt_version,
            "planner_contract_version": planning.planner_contract_version,
            "attempts": [
                {"ordinal": a.ordinal, "kind": a.kind, "outcome": a.outcome,
                 "usage": a.usage.as_dict(), "error": a.error}
                for a in planning.attempts
            ],
            "usage": usage,
            "repaired": planning.repaired,
        }
        # Checkpointed with the plan, so a run resumed after a crash reports
        # the planning it did not have to repeat.
        return {"route": "check", "plan": planning.plan.model_dump(mode="json"),
                "planning": summary,
                "model_calls": int(state.get("model_calls") or 0) + len(planning.attempts)}

    def _adopt_planning(self, state: dict[str, Any]) -> None:
        """Request-local planning facts, from the checkpoint -- the plan may
        have been made by the request that died, not this one."""
        summary = state.get("planning") or {}
        if not summary or self.planning_summary is not None:
            return
        self.planning_summary = summary
        usage = summary.get("usage") or {}
        # Written whether or not usage is known, so the audit distinguishes
        # "no tokens reported" from "this field was never populated".
        self.audit["input_tokens"] = usage.get("input_tokens")
        self.audit["output_tokens"] = usage.get("output_tokens")
        self.audit["usage_known"] = usage.get("known")
        self.audit["model_id"] = summary.get("model_id") or summary.get("provider")
        self.audit["prompt_version"] = summary.get("prompt_version")
        self.audit["planner_attempts"] = len(summary.get("attempts") or [])
        self.audit["planner_repaired"] = summary.get("repaired")

    def node_check(self, state: dict[str, Any]) -> dict[str, Any]:
        """Does the plan answer the question -- and may this caller have it?"""
        self.guard(state)
        self._adopt_planning(state)
        plan = AnalyticalPlan.model_validate(state["plan"])
        question = state["effective_question"]
        self.audit["plan_hash"] = plan.fingerprint()
        self.audit.setdefault("turn_kind", self.continuity(question).kind.value)
        telemetry.annotate(**{"pac.metric": plan.metric.value,
                              "pac.turn_kind": self.audit["turn_kind"]})

        # A plan can be valid, compile cleanly and return a confident number
        # for a DIFFERENT question, and nothing downstream can tell. Checked
        # here rather than inside a planner so it holds for every planner.
        gaps = find_gaps(question, plan, self.vocab, self.index,
                         resolved=state.get("chosen") or {})
        blockers = blocking(gaps)
        self.audit["intent_gaps"] = [g.kind for g in gaps]
        self.audit["blocking_gaps"] = [g.kind for g in blockers]
        plan_json = plan.model_dump(mode="json")
        if blockers:
            # Answering would silently broaden the question: drop an
            # unresolvable product filter and the reply is the whole company's
            # volume presented as that product's.
            return self._clarify(" ".join(g.message() for g in blockers),
                                 reason=",".join(sorted({g.kind for g in blockers})),
                                 plan=plan_json, stage_plan=True)
        if plan.clarification:
            self.stage(None, plan.clarification, "clarify")
            self.finish(PipelineResult(status="clarify",
                                       conversation_id=self.state.conversation_id,
                                       message=plan.clarification, plan=plan_json),
                        "clarify")
            return {"route": "end", "outcome": "clarify"}

        # Current access, from THIS request's principal. Terminal: a refusal
        # is never retried under different access.
        try:
            with telemetry.span("pac.policy", expected=(AuthorizationError,)):
                authorize(plan, self.principal)
        except AuthorizationError as exc:
            self.stage(plan_json, str(exc), "denied")
            self.finish(PipelineResult(
                status="denied", conversation_id=self.state.conversation_id,
                message=str(exc), alternative=exc.alternative, plan=plan_json,
            ), "denied", denial_reason=str(exc)[:200])
            return {"route": "end", "outcome": "denied"}
        return {"route": "answer", "disclosures": [g.message() for g in gaps]}

    def node_answer(self, state: dict[str, Any]) -> dict[str, Any]:
        """Compile, validate, execute, render and commit -- in one node, so
        rows and headlines (which can carry revenue) never pass through a
        checkpoint."""
        self.guard(state)
        self._adopt_planning(state)
        principal = self.principal
        plan = AnalyticalPlan.model_validate(state["plan"])
        continuity = self.continuity(state["effective_question"])

        # A frozen cohort is applied by the server, whole. The typed plan's
        # filter holds 200 ids; a 500-account answer followed by "those same
        # accounts" means all 500. Deterministic, after planning: whether the
        # model copied ids into its plan or not, the population is the stored
        # one, and any ids it did copy are replaced rather than intersected.
        binding = None
        if continuity.carries_cohort and continuity.cohort is not None:
            binding = CohortBinding(dimension=continuity.cohort.dimension,
                                    ids=tuple(continuity.cohort.ids))
            self.applied = (binding.dimension, binding.ids)
            field_name = continuity.cohort.filter_field
            if field_name and getattr(plan.filters, field_name, None):
                plan = plan.model_copy(update={
                    "filters": plan.filters.model_copy(update={field_name: []})})
        plan_json = plan.model_dump(mode="json")

        def fail(message: str, status: str, reason: str, *,
                 result_status: str = "error", staged: bool = False) -> dict[str, Any]:
            if staged:
                self.stage(plan_json, message, "error")
            self.finish(PipelineResult(status=result_status,
                                       conversation_id=self.state.conversation_id,
                                       message=message, plan=plan_json),
                        status, denial_reason=reason[:200])
            return {"route": "end", "outcome": result_status}

        try:
            with telemetry.span("pac.compile", expected=(UnsupportedCombination,)):
                query = self.pipe.compiler.compile(plan, anchor=self.anchor, cohort=binding)
        except UnsupportedCombination as exc:
            # Nothing is broken: the question combines things that have no
            # defined meaning together. Reported as an error, it told the user
            # the system had failed and counted as an execution failure.
            return fail("That combination cannot be answered as asked. " + " ".join(exc.reasons),
                        "unsupported_combination", str(exc), result_status="clarify")
        except (CompileError, PeriodError) as exc:
            return fail(f"I could not build that query: {exc}", "compile_error", str(exc))
        self.audit["sql_hash"] = query.fingerprint()

        # Runs on the FINAL text, after every rewrite, and the same text is
        # what executes below.
        try:
            with telemetry.span("pac.validate"):
                validate(query.sql, wac_authorized=principal.wac_authorized)
        except SqlValidationError as exc:
            log.error("compiler produced SQL that failed validation: %s", exc)
            return fail("I could not run that safely, so I stopped before querying.",
                        "validation_error", str(exc))

        self.guard(state)
        t0 = time.perf_counter()
        try:
            # One snapshot: the published generation is checked and the facts
            # read in the same repeatable-read transaction, so the rows are the
            # generation this request planned against -- its calendar, its
            # vocabulary, its anchor -- or the query does not run.
            with telemetry.span("pac.sql", expected=(GenerationChanged,)) as sql_span, \
                    analytics_transaction(
                        scope_kind=principal.scope_kind,
                        scope_value=principal.scope_value,
                        wac_authorized=principal.wac_authorized,
                        expect_generation=self.dataset["dataset_id"],
                    ) as cur:
                cur.execute(query.sql, query.params)
                rows = cur.fetchall()
                sql_span.set(**{"pac.row_count": len(rows)})
        except GenerationChanged as exc:
            # A refresh landed while this was being planned. The plan's
            # vocabulary and calendar describe the old data, so it is not run.
            # The run is closed as failed: a retry with the same key runs
            # again, against the new generation.
            log.info("generation changed mid-request (%s)", exc)
            telemetry.count("pac.db.errors", kind="generation_changed")
            self.finish(PipelineResult(
                status="refresh", conversation_id=self.state.conversation_id,
                message=("The data was refreshed while your question was being "
                         "answered. Asking again will use the latest data."),
            ), "generation_changed", denial_reason=str(exc)[:200])
            return {"route": "end", "outcome": "refresh"}
        except ScopeBindingError as exc:
            self.finish(PipelineResult(
                status="denied", conversation_id=self.state.conversation_id,
                message=("Your account does not have a usable data scope, so no "
                         "data can be shown."),
            ), "scope_error", denial_reason=str(exc)[:200])
            return {"route": "end", "outcome": "denied"}
        except Exception as exc:  # database timeout, cancellation, unavailability
            name = type(exc).__name__
            log.warning("query failed (%s): %s", name, exc)
            # By type, not by name: "PoolTimeout" is no connection, not a
            # slow query, and telling that user to narrow the question was
            # wrong advice.
            slow = isinstance(exc, psycopg.errors.QueryCanceled)
            telemetry.count("pac.db.errors", kind="timeout" if slow else "unavailable")
            friendly = (
                "That question took too long to answer. Narrowing it — a shorter time "
                "period, a specific product, or fewer groupings — will usually work."
                if slow
                else "The data service is temporarily unavailable. Please try again."
            )
            return fail(friendly, "db_error", name)
        self.timings["db_ms"] = int((time.perf_counter() - t0) * 1000)
        self.audit["db_ms"] = self.timings["db_ms"]
        self.audit["row_count"] = len(rows)

        try:
            with telemetry.span("pac.render") as render_span:
                answer = render(
                    rows, query, plan,
                    scope_note=scope_note(principal, plan),
                    max_rows=self.pipe.settings.max_result_rows,
                    source_coverage=self.dataset.get("source_coverage") or {},
                    max_bytes=self.pipe.settings.max_result_bytes,
                )
                render_span.set(**{"pac.truncated": bool(answer.truncated)})
        except GrainError as exc:
            # The rows are not at the grain the plan declared, so the table
            # would read as more groups than there are. Fails closed.
            log.error("grain violation for request %s: %s", self.request_id, exc)
            return fail("That result did not pass an internal consistency check, so "
                        "it is not being shown. This has been logged.",
                        "grain_error", str(exc), staged=True)
        if self.state.reset_reason:
            answer.notes.insert(0, self.state.reset_reason)
        if plan.interpretation:
            answer.notes.insert(0, plan.interpretation)
        # Non-blocking gaps: the number is true, it just is not the whole
        # question. Said first, because it changes how the figure reads.
        for disclosure in reversed(state.get("disclosures") or []):
            answer.notes.insert(0, disclosure)
        # An inherited filter is never applied silently.
        for disclosure in reversed(continuity.disclosures):
            answer.notes.insert(0, disclosure)

        # What a later "those" may refer to: the whole, distinct population
        # shown -- bounded by the response cap and by nothing else. Committed
        # by finish(), atomically with the turn.
        summary = summarise_cohort(
            rows,
            dimension=plan.dimensions[0].value if plan.dimensions else None,
            max_rows=self.pipe.settings.max_result_rows,
        )
        self.stage(
            plan_json, answer.headline, "answered",
            cohort_dimension=summary.dimension if summary else None,
            cohort_ids=list(summary.ids) if summary else [],
            cohort_complete=summary.complete if summary else True,
            cohort_total=summary.total_available if summary else None,
        )
        self.finish(PipelineResult(
            applied_cohort=self.applied,
            status="answered", conversation_id=self.state.conversation_id,
            message=answer.headline, answer=answer,
            interpretation=plan.interpretation,
            plan=plan_json,
            # SQL is returned only on explicit request, and only the SQL this
            # principal was authorized to run.
            sql=query.sql if self.include_sql else None,
        ), "answered")
        return {"route": "end", "outcome": "answered"}
