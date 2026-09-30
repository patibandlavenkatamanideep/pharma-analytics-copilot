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
        idempotency_key: str | None = None,
    ) -> PipelineResult:
        request_id = uuid.uuid4().hex[:16]
        started = time.perf_counter()
        timings: dict[str, int] = {}
        dataset = self.current_dataset()
        anchor = dataset["reporting_anchor"]
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
        state = open_conversation(
            principal, conversation_id,
            dataset_id=dataset["dataset_id"],
            metric_version=get_registry().version,
            policy_version=POLICY_VERSION,
        )

        # One live run per conversation, and one committed outcome per key.
        # Raises RunBusy / IdempotencyConflict / ReplayUnavailable, which the
        # API maps to 409 or 403.
        run = runs.acquire(
            principal, state.conversation_id,
            revision=state.revision, request_hash=request_hash,
            idempotency_key=idempotency_key,
            lease_seconds=self.settings.run_lease_seconds,
            retention_seconds=self.settings.idempotency_retention_seconds,
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

        # What this request will commit. Every exit path stages at most one
        # turn; finish() writes it -- and the run's outcome, the cohort, the
        # clarification and the revision -- in ONE transaction.
        staged: dict[str, StagedTurn] = {}

        def stage(plan: dict[str, Any] | None, answer_text: str, status: str,
                  **extra: Any) -> None:
            staged["turn"] = StagedTurn(
                question=question, plan=plan, answer_text=answer_text,
                status=status, resolves_clarification=resolving, **extra)

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
            result.run_id = run.run_id
            if result.planning is None:
                result.planning = _planning_summary.get("value")

            turn = staged.get("turn")
            if turn is None:
                # Nothing to record -- a failure before an outcome existed.
                # The run is closed so its key can be retried.
                result.persistence = "not_saved"
                result.payload = to_payload(result, include_sql)
                runs.fail(run, None)
            else:
                result.persistence = "saved"
                result.payload = to_payload(result, include_sql)
                try:
                    done = finalise(principal, state, run, turn, result.payload)
                except Exception:
                    # Stated, not swallowed: the answer is returned, and the
                    # response says it was not saved, so nothing implies the
                    # next turn can build on it.
                    log.exception("failed to commit turn for run %s", run.run_id)
                    runs.fail(run, None)
                    done = Finalised(persisted=False, reason="error")
                if done.conflict:
                    # The conversation moved while this was being answered: a
                    # lease expired and another turn committed. The answer was
                    # planned against state that is no longer current, so it
                    # is withheld rather than shown as a continuation.
                    status = "conflict"
                    result = PipelineResult(
                        status="conflict", conversation_id=state.conversation_id,
                        message=("This conversation moved on while that was being "
                                 "answered. Ask again to continue from the latest turn."),
                        request_id=request_id, run_id=run.run_id,
                        timings=timings, persistence="conflict")
                    result.payload = to_payload(result, include_sql)
                elif not done.persisted:
                    result.persistence = "failed"
                    result.payload = to_payload(result, include_sql)
            audit.update(status=status, total_ms=timings["total_ms"], **extra)
            self._write_audit(audit)
            return result

        # --- 3-4. plan ------------------------------------------------------
        # The dataset is already resolved for this request; passing it keeps
        # the vocabulary cache keyed to the snapshot being queried without a
        # second lookup.
        vocab = vocabulary_for(principal, dataset["dataset_id"])
        index = entity_index(principal, dataset["dataset_id"], vocab)
        from app.llm.planner import PlanningContext

        # --- 2b. a reply to a pending clarification ----------------------------
        # "Riverside Clinic is the name of 2 accounts -- which one?" followed by
        # "the second one". The choices were stored as shown; the reply is read
        # against them, and the ORIGINAL question is re-run with the choice
        # bound. The choice is re-checked against the caller's CURRENT index:
        # access can change between a question and its answer, and a stored
        # option is not a grant.
        effective_question = question
        chosen: dict[str, str] = {}
        resolving: str | None = None
        pending = state.pending_clarification
        if pending is not None:
            picked = choice_from_reply(question, pending.choices)
            if picked is not None:
                option = pending.choices[picked]
                if option["id"] not in index.account_ids:
                    message = ("That option is no longer available to you. "
                               "Ask the question again to see current options.")
                    stage(None, message, "clarify")
                    return finish(
                        PipelineResult(status="clarify",
                                       conversation_id=state.conversation_id,
                                       message=message),
                        "clarify", denial_reason="clarification_choice_unavailable")
                effective_question = pending.question
                chosen = {normalise(pending.slot_text or ""): option["id"]}
                resolving = pending.clarification_id

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
            effective_question,
            previous_plan=state.previous_plan,
            cohort=cohort_obj,
        )
        audit["turn_kind"] = continuity.kind.value

        if continuity.clarification:
            # An ambiguous reference is asked about, not guessed at.
            stage(None, continuity.clarification, "clarify")
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
        mentions, unresolved = resolve_mentions(effective_question, vocab, index,
                                                resolved=chosen)
        if unresolved:
            audit["intent_gaps"] = [g.kind for g in unresolved]
            audit["blocking_gaps"] = [g.kind for g in unresolved]
            message = " ".join(g.message() for g in unresolved)
            # One question at a time: the first ambiguous name is stored with
            # the choices exactly as shown, so "the second one" means what the
            # user saw second. A later ambiguity is asked about on the re-run.
            asking = next((g for g in unresolved if g.choices), None)
            choices = ([{"id": c[0], "label": c[1], "detail": c[2]} for c in asking.choices]
                       if asking else [])
            stage(None, message, "clarify", clarification=(
                {"kind": asking.kind, "question": effective_question,
                 "slot_text": asking.subject, "choices": choices} if asking else None))
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
            planning = self.planner.plan(effective_question, context)
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
        gaps = find_gaps(effective_question, plan, vocab, index, resolved=chosen)
        blockers = blocking(gaps)
        audit["intent_gaps"] = [g.kind for g in gaps]
        audit["blocking_gaps"] = [g.kind for g in blockers]
        if blockers:
            # Answering would silently broaden the question: drop an
            # unresolvable product filter and the reply is the whole company's
            # volume presented as that product's.
            message = " ".join(g.message() for g in blockers)
            stage(plan.model_dump(mode="json"), message, "clarify")
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
            stage(None, plan.clarification, "clarify")
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
            stage(plan.model_dump(mode="json"), str(exc), "denied")
            return finish(
                PipelineResult(
                    status="denied", conversation_id=state.conversation_id,
                    message=str(exc), alternative=exc.alternative,
                    plan=plan.model_dump(mode="json"),
                ),
                "denied", denial_reason=str(exc)[:200],
            )

        # --- 7. compile -----------------------------------------------------
        # A frozen cohort is applied by the server, whole. The typed plan's
        # filter holds 200 ids; a 500-account answer followed by "those same
        # accounts" means all 500. Deterministic, after planning: whether the
        # model copied ids into its plan or not, the population is the stored
        # one, and any ids it did copy are replaced rather than intersected.
        binding = None
        if continuity.carries_cohort and continuity.cohort is not None:
            binding = CohortBinding(dimension=continuity.cohort.dimension,
                                    ids=tuple(continuity.cohort.ids))
            field_name = continuity.cohort.filter_field
            if field_name and getattr(plan.filters, field_name, None):
                plan = plan.model_copy(update={
                    "filters": plan.filters.model_copy(update={field_name: []})})
        try:
            query = self.compiler.compile(plan, anchor=anchor, cohort=binding)
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
            stage(plan.model_dump(mode="json"), "internal consistency check failed", "error")
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
        # One rule, in continuity.py, for what a later "those" may refer to:
        # the whole, distinct population shown -- bounded by the response cap
        # and by nothing else. Committed by finish(), atomically with the turn.
        summary = summarise_cohort(
            rows,
            dimension=plan.dimensions[0].value if plan.dimensions else None,
            max_rows=self.settings.max_result_rows,
        )
        stage(
            plan.model_dump(mode="json"), answer.headline, "answered",
            cohort_dimension=summary.dimension if summary else None,
            cohort_ids=list(summary.ids) if summary else [],
            cohort_complete=summary.complete if summary else True,
            cohort_total=summary.total_available if summary else None,
        )

        return finish(
            PipelineResult(
                applied_cohort=(binding.dimension, binding.ids) if binding else None,
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
