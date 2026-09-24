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
from dataclasses import dataclass, field
from typing import Any

from app.analytics.compiler import Compiler, CompileError
from app.analytics.entities import vocabulary_for
from app.analytics.periods import PeriodError
from app.analytics.plan import AnalyticalPlan
from app.analytics.registry import get_registry
from app.analytics.render import Answer, render
from app.analytics.validator import SqlValidationError, validate
from app.auth.policy import AuthorizationError, Principal, authorize, scope_note
from app.config import get_settings
from app.conversation.state import ConversationState, open_conversation, record_turn
from app.db import ScopeBindingError, analytics_transaction, auth_transaction, owner_transaction
from app.llm.planner import Planner, PlannerError

log = logging.getLogger(__name__)

POLICY_VERSION = "1.0.0"


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


class Pipeline:
    def __init__(self, planner: Planner) -> None:
        self.planner = planner
        self.settings = get_settings()
        self.compiler = Compiler(max_rows=self.settings.max_result_rows)

    # -- dataset manifest ----------------------------------------------------

    def current_dataset(self) -> dict[str, Any]:
        """The published snapshot. A partially loaded refresh is never visible."""
        with owner_transaction() as cur:
            cur.execute(
                "SELECT dataset_id, load_mode, reporting_anchor, row_counts, warnings, "
                "       published_at, source_coverage "
                "FROM app_meta.dataset_manifest WHERE load_state = 'published' "
                "ORDER BY published_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("no published dataset -- run scripts/load_data.py")
        return dict(row)

    # -- main entry point ----------------------------------------------------

    def ask(
        self,
        principal: Principal,
        question: str,
        *,
        conversation_id: str | None = None,
        include_sql: bool = False,
    ) -> PipelineResult:
        request_id = uuid.uuid4().hex[:16]
        started = time.perf_counter()
        timings: dict[str, int] = {}
        dataset = self.current_dataset()
        anchor = dataset["reporting_anchor"]

        state = open_conversation(principal, conversation_id)

        audit: dict[str, Any] = {
            "request_id": request_id,
            "user_id": principal.user_id,
            "role": principal.role,
            "scope_kind": principal.scope_kind,
            "scope_value": principal.scope_value,
            "wac_authorized": principal.wac_authorized,
            "dataset_id": dataset["dataset_id"],
            "metric_version": get_registry().version,
            "policy_version": POLICY_VERSION,
        }

        def finish(result: PipelineResult, status: str, **extra: Any) -> PipelineResult:
            timings["total_ms"] = int((time.perf_counter() - started) * 1000)
            result.timings = timings
            result.request_id = request_id
            audit.update(status=status, total_ms=timings["total_ms"], **extra)
            self._write_audit(audit)
            return result

        # --- 3-4. plan ------------------------------------------------------
        vocab = vocabulary_for(principal)
        from app.llm.planner import PlanningContext

        context = PlanningContext(
            role=principal.role,
            scope_description=principal.scope_description,
            wac_authorized=principal.wac_authorized,
            reporting_anchor=anchor,
            known_products=vocab.products,
            known_subcategories=vocab.subcategories,
            known_categories=vocab.categories,
            known_gpos=vocab.gpos,
            known_archetypes=vocab.archetypes,
            known_territories=vocab.territories,
            known_regions=vocab.regions,
            all_territories=vocab.all_territories,
            all_regions=vocab.all_regions,
            previous_plan=state.previous_plan,
            previous_cohort=state.previous_cohort,
        )

        t0 = time.perf_counter()
        try:
            plan = self.planner.plan(question, context)
        except PlannerError as exc:
            log.warning("planner failed: %s", exc)
            return finish(
                PipelineResult(
                    status="error", conversation_id=state.conversation_id,
                    message=(
                        "I could not interpret that question. Try naming the metric, the "
                        "product or account, and the time period — for example "
                        "'top 10 accounts by pack units last quarter'."
                    ),
                ),
                "planner_error", denial_reason=str(exc)[:200],
            )
        timings["plan_ms"] = int((time.perf_counter() - t0) * 1000)
        audit["plan_hash"] = plan.fingerprint()

        usage = getattr(self.planner, "last_usage", None) or {}
        if usage:
            audit["input_tokens"] = usage.get("input_tokens")
            audit["output_tokens"] = usage.get("output_tokens")
        audit["model_id"] = getattr(self.planner, "model_id", "offline")

        # --- 5. clarify -----------------------------------------------------
        if plan.clarification:
            self._record(principal, state, question, None, [], plan.clarification, "clarify")
            return finish(
                PipelineResult(
                    status="clarify", conversation_id=state.conversation_id,
                    message=plan.clarification, plan=plan.model_dump(mode="json"),
                ),
                "clarify",
            )

        # --- 6. authorize ---------------------------------------------------
        try:
            authorize(plan, principal)
        except AuthorizationError as exc:
            self._record(principal, state, question, plan.model_dump(mode="json"),
                         [], str(exc), "denied")
            return finish(
                PipelineResult(
                    status="denied", conversation_id=state.conversation_id,
                    message=str(exc), alternative=exc.alternative,
                    plan=plan.model_dump(mode="json"),
                ),
                "denied", denial_reason=str(exc)[:200],
            )

        # --- 7. compile -----------------------------------------------------
        try:
            query = self.compiler.compile(plan, anchor=anchor)
        except (CompileError, PeriodError) as exc:
            return finish(
                PipelineResult(
                    status="error", conversation_id=state.conversation_id,
                    message=f"I could not build that query: {exc}",
                    plan=plan.model_dump(mode="json"),
                ),
                "compile_error", denial_reason=str(exc)[:200],
            )
        audit["sql_hash"] = query.fingerprint()

        # --- 8. validate ----------------------------------------------------
        # Runs on the FINAL text, after every rewrite, and the same text is what
        # executes below.
        try:
            validate(query.sql, wac_authorized=principal.wac_authorized)
        except SqlValidationError as exc:
            log.error("compiler produced SQL that failed validation: %s", exc)
            return finish(
                PipelineResult(
                    status="error", conversation_id=state.conversation_id,
                    message="I could not run that safely, so I stopped before querying.",
                    plan=plan.model_dump(mode="json"),
                ),
                "validation_error", denial_reason=str(exc)[:200],
            )

        # --- 9. execute -----------------------------------------------------
        t0 = time.perf_counter()
        try:
            with analytics_transaction(
                scope_kind=principal.scope_kind,
                scope_value=principal.scope_value,
                wac_authorized=principal.wac_authorized,
            ) as cur:
                cur.execute(query.sql, query.params)
                rows = cur.fetchall()
        except ScopeBindingError as exc:
            return finish(
                PipelineResult(
                    status="denied", conversation_id=state.conversation_id,
                    message=("Your account does not have a usable data scope, so no "
                             "data can be shown."),
                ),
                "scope_error", denial_reason=str(exc)[:200],
            )
        except Exception as exc:  # database timeout, cancellation, unavailability
            name = type(exc).__name__
            log.warning("query failed (%s): %s", name, exc)
            friendly = (
                "That question took too long to answer. Narrowing it — a shorter time "
                "period, a specific product, or fewer groupings — will usually work."
                if "Timeout" in name or "QueryCanceled" in name
                else "The data service is temporarily unavailable. Please try again."
            )
            return finish(
                PipelineResult(
                    status="error", conversation_id=state.conversation_id,
                    message=friendly, plan=plan.model_dump(mode="json"),
                ),
                "db_error", denial_reason=name,
            )
        timings["db_ms"] = int((time.perf_counter() - t0) * 1000)
        audit["db_ms"] = timings["db_ms"]
        audit["row_count"] = len(rows)

        # --- 10. render -----------------------------------------------------
        answer = render(
            rows, query, plan,
            scope_note=scope_note(principal, plan),
            max_rows=self.settings.max_result_rows,
        )
        if state.reset_reason:
            answer.notes.insert(0, state.reset_reason)
        if plan.interpretation:
            answer.notes.insert(0, plan.interpretation)

        # --- 11. persist ----------------------------------------------------
        cohort = [
            str(r["dim0_id"]) for r in rows[:200]
            if r.get("dim0_id") is not None
        ] if plan.dimensions else []
        self._record(
            principal, state, question, plan.model_dump(mode="json"),
            cohort, answer.headline, "answered",
        )

        return finish(
            PipelineResult(
                status="answered", conversation_id=state.conversation_id,
                message=answer.headline, answer=answer,
                interpretation=plan.interpretation,
                plan=plan.model_dump(mode="json"),
                # SQL is returned only on explicit request, and only the SQL
                # this principal was authorized to run.
                sql=query.sql if include_sql else None,
            ),
            "answered",
        )

    # -- helpers -------------------------------------------------------------

    def _record(
        self, principal: Principal, state: ConversationState, question: str,
        plan: dict[str, Any] | None, cohort: list[str], answer_text: str, status: str,
    ) -> None:
        try:
            record_turn(
                principal, state, question=question, plan=plan,
                cohort=cohort, answer_text=answer_text, status=status,
            )
        except Exception:                       # never fail a request on bookkeeping
            log.warning("failed to persist conversation turn", exc_info=True)

    def _write_audit(self, audit: dict[str, Any]) -> None:
        """Hashes, counts and timings only -- never WAC values, result rows or prompts."""
        columns = [
            "request_id", "user_id", "role", "scope_kind", "scope_value", "wac_authorized",
            "dataset_id", "metric_version", "policy_version", "plan_hash", "sql_hash",
            "status", "denial_reason", "row_count", "db_ms", "total_ms", "model_id",
            "input_tokens", "output_tokens",
        ]
        values = [audit.get(c) for c in columns]
        placeholders = ", ".join(["%s"] * len(columns))
        try:
            with auth_transaction() as cur:
                cur.execute(
                    f"INSERT INTO app_meta.query_audit ({', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
        except Exception:
            log.warning("failed to write audit row", exc_info=True)
