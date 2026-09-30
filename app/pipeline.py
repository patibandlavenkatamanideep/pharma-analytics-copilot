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

from app.analytics.compiler import Compiler, CompileError, UnsupportedCombination
from app.analytics.entities import vocabulary_for
from app.analytics.intent import blocking, find_gaps, resolve_mentions
from app.analytics.mentions import entity_index
from app.conversation.continuity import Cohort, summarise_cohort
from app.conversation.continuity import resolve as resolve_continuity
from app.analytics.periods import PeriodError
from app.analytics.registry import get_registry
from app.analytics.render import Answer, GrainError, render
from app.analytics.validator import SqlValidationError, validate
from app.auth.policy import AuthorizationError, Principal, authorize, scope_note
from app.config import get_settings
from app.conversation.state import ConversationState, open_conversation, record_turn
from app.db import ScopeBindingError, analytics_transaction, auth_transaction
from app.llm.planner import Planner, PlannerError

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


class Pipeline:
    def __init__(self, planner: Planner) -> None:
        self.planner = planner
        self.settings = get_settings()
        self.compiler = Compiler(max_rows=self.settings.max_result_rows)

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

        # Continuation is bound to the dataset and the semantic contracts, not
        # only to who is asking: a refresh or a contract bump makes a carried
        # plan incomparable rather than merely old.
        state = open_conversation(
            principal, conversation_id,
            dataset_id=dataset["dataset_id"],
            metric_version=get_registry().version,
            policy_version=POLICY_VERSION,
        )

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

        # Filled once planning completes; finish() attaches it to whatever
        # result is returned, including the failure paths.
        _planning_summary: dict[str, Any] = {}

        def finish(result: PipelineResult, status: str, **extra: Any) -> PipelineResult:
            timings["total_ms"] = int((time.perf_counter() - started) * 1000)
            result.timings = timings
            result.request_id = request_id
            if result.planning is None:
                result.planning = _planning_summary.get("value")
            audit.update(status=status, total_ms=timings["total_ms"], **extra)
            self._write_audit(audit)
            return result

        # --- 3-4. plan ------------------------------------------------------
        # The dataset is already resolved for this request; passing it keeps
        # the vocabulary cache keyed to the snapshot being queried without a
        # second lookup.
        vocab = vocabulary_for(principal, dataset["dataset_id"])
        from app.llm.planner import PlanningContext

        # One decision about what this turn is, shared by every planner.
        # Previously the offline planner and the live prompt each decided
        # separately, and disagreed.
        cohort_obj = None
        if state.previous_cohort:
            cohort_obj = Cohort(
                dimension=state.previous_cohort_dimension or "",
                ids=tuple(state.previous_cohort),
                dataset_id=dataset["dataset_id"],
                complete=state.previous_cohort_complete,
                total_available=state.previous_cohort_total,
            )
        continuity = resolve_continuity(
            question,
            previous_plan=state.previous_plan,
            cohort=cohort_obj,
        )
        audit["turn_kind"] = continuity.kind.value

        if continuity.clarification:
            # An ambiguous reference is asked about, not guessed at.
            self._record(principal, state, question, None, [],
                         continuity.clarification, "clarify")
            return finish(
                PipelineResult(
                    status="clarify", conversation_id=state.conversation_id,
                    message=continuity.clarification,
                ),
                "clarify", denial_reason=f"ambiguous:{continuity.kind.value}",
            )

        # --- 3b. entities the question names --------------------------------
        # Resolved on the server, under the caller's scope, BEFORE planning.
        # An unknown or ambiguous name has no plan that could fix it, so it is
        # asked about without spending a model call; and the ids of accounts
        # the question names -- those ids and no others -- go to the planner,
        # which never sees the account catalog.
        index = entity_index(principal, dataset["dataset_id"], vocab)
        mentions, unresolved = resolve_mentions(question, vocab, index)
        if unresolved:
            audit["intent_gaps"] = [g.kind for g in unresolved]
            audit["blocking_gaps"] = [g.kind for g in unresolved]
            message = " ".join(g.message() for g in unresolved)
            choices = [{"id": c[0], "label": c[1], "detail": c[2]}
                       for g in unresolved for c in g.choices]
            self._record(principal, state, question, None, [], message, "clarify")
            return finish(
                PipelineResult(
                    status="clarify", conversation_id=state.conversation_id,
                    message=message, choices=choices,
                ),
                "clarify",
                denial_reason=",".join(sorted({g.kind for g in unresolved})),
            )
        named_accounts = [
            (m.text, m.ids[0]) for m in mentions
            if m.kind == "account" and len(m.ids) == 1 and not m.reference_only
        ]

        context = PlanningContext(
            role=principal.role,
            scope_description=principal.scope_description,
            wac_authorized=principal.wac_authorized,
            reporting_anchor=anchor,
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
            previous_plan=state.previous_plan,
            previous_cohort=state.previous_cohort,
            previous_cohort_dimension=state.previous_cohort_dimension,
            previous_cohort_complete=state.previous_cohort_complete,
            previous_cohort_total=state.previous_cohort_total,
            continuity=continuity,
            named_accounts=named_accounts,
        )

        t0 = time.perf_counter()
        try:
            planning = self.planner.plan(question, context)
            plan = planning.plan
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

        # Request-local, from THIS call. Previously read off the planner
        # instance, which one concurrent request could overwrite for another.
        usage = planning.usage.as_dict()
        # Written whether or not usage is known, so the audit distinguishes
        # "no tokens reported" from "this field was never populated".
        audit["input_tokens"] = usage.get("input_tokens")
        audit["output_tokens"] = usage.get("output_tokens")
        audit["usage_known"] = usage.get("known")
        audit["model_id"] = planning.model_id or planning.provider
        audit["prompt_version"] = planning.prompt_version
        audit["planner_attempts"] = len(planning.attempts)
        audit["planner_repaired"] = planning.repaired
        planning_summary = {
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
        _planning_summary["value"] = planning_summary

        # --- 4b. intent fidelity ---------------------------------------------
        # Does the plan answer the question that was asked? A plan can be
        # valid, compile cleanly and return a confident number for a DIFFERENT
        # question, and nothing downstream can tell: the SQL, the scope and the
        # rendering are all correct. Checked here rather than inside a planner
        # so it holds for every planner, including a model that drops a filter
        # it could not resolve.
        gaps = find_gaps(question, plan, vocab, index)
        blockers = blocking(gaps)
        audit["intent_gaps"] = [g.kind for g in gaps]
        audit["blocking_gaps"] = [g.kind for g in blockers]
        if blockers:
            # Answering would silently broaden the question: drop an
            # unresolvable product filter and the reply is the whole company's
            # volume presented as that product's.
            message = " ".join(g.message() for g in blockers)
            self._record(principal, state, question, plan.model_dump(mode="json"),
                         [], message, "clarify")
            return finish(
                PipelineResult(
                    status="clarify", conversation_id=state.conversation_id,
                    message=message, plan=plan.model_dump(mode="json"),
                ),
                # The kinds, not "unresolved_entity" for everything: a
                # threshold that lost its direction is not an unknown entity.
                "clarify", denial_reason=",".join(sorted({g.kind for g in blockers})),
            )
        disclosures = [g.message() for g in gaps]

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
        except UnsupportedCombination as exc:
            # Nothing is broken: the question combines things that have no
            # defined meaning together. Reporting that as an error told the
            # user the system had failed, and counted as an execution
            # failure in evaluation.
            return finish(
                PipelineResult(
                    status="clarify", conversation_id=state.conversation_id,
                    message=(
                        "That combination cannot be answered as asked. "
                        + " ".join(exc.reasons)
                    ),
                    plan=plan.model_dump(mode="json"),
                ),
                "unsupported_combination", denial_reason=str(exc)[:200],
            )
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
        try:
            answer = render(
                rows, query, plan,
                scope_note=scope_note(principal, plan),
                max_rows=self.settings.max_result_rows,
                source_coverage=dataset.get("source_coverage") or {},
                max_bytes=self.settings.max_result_bytes,
            )
        except GrainError as exc:
            # The rows are not at the grain the plan declared, so the table
            # would read as more groups than there are. This is our bug, not
            # the user's question -- but showing a wrong table is worse than
            # showing none, so it fails closed and is logged with the plan.
            log.error("grain violation for request %s: %s", request_id, exc)
            self._record(principal, state, question, plan.model_dump(mode="json"),
                         [], "internal consistency check failed", "error")
            return finish(
                PipelineResult(
                    status="error", conversation_id=state.conversation_id,
                    message=(
                        "That result did not pass an internal consistency check, so "
                        "it is not being shown. This has been logged."
                    ),
                    plan=plan.model_dump(mode="json"),
                ),
                "grain_error", denial_reason=str(exc)[:200],
            )
        if state.reset_reason:
            answer.notes.insert(0, state.reset_reason)
        if plan.interpretation:
            answer.notes.insert(0, plan.interpretation)
        # Non-blocking gaps: the number is true, it just is not the whole
        # question. Said first, because it changes how the figure reads.
        for disclosure in reversed(disclosures):
            answer.notes.insert(0, disclosure)
        # An inherited filter is never applied silently.
        for disclosure in reversed(continuity.disclosures):
            answer.notes.insert(0, disclosure)

        # --- 11. persist ----------------------------------------------------
        # One rule, in continuity.py, for all three limits that can cut a
        # cohort down: the ranking limit, the response cap and the storage
        # cap. Computing it here from `rows` alone got two of them wrong --
        # it counted the renderer's discarded probe row, and it could not
        # see truncation at all when max_result_rows was below the storage
        # cap.
        summary = summarise_cohort(
            rows,
            dimension=plan.dimensions[0].value if plan.dimensions else None,
            max_rows=self.settings.max_result_rows,
        )
        self._record(
            principal, state, question, plan.model_dump(mode="json"),
            list(summary.ids) if summary else [], answer.headline, "answered",
            cohort_dimension=summary.dimension if summary else None,
            cohort_complete=summary.complete if summary else True,
            cohort_total=summary.total_available if summary else None,
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
        cohort_dimension: str | None = None,
        cohort_complete: bool = True, cohort_total: int | None = None,
    ) -> None:
        try:
            record_turn(
                principal, state, question=question, plan=plan,
                cohort=cohort, answer_text=answer_text, status=status,
                cohort_dimension=cohort_dimension,
                cohort_complete=cohort_complete, cohort_total=cohort_total,
            )
        except Exception:                       # never fail a request on bookkeeping
            log.warning("failed to persist conversation turn", exc_info=True)

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
            with auth_transaction() as cur:
                cur.execute(
                    f"INSERT INTO app_meta.query_audit ({', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
        except Exception:
            log.warning("failed to write audit row", exc_info=True)
