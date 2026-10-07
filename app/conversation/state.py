"""Owner-scoped conversation state.

What is persisted is the structured plan and the resolved entity ids, never SQL
and never result rows. A follow-up therefore patches a typed object rather than
re-parsing old prose, and old text can never overwrite who the user is.

Authorization here is CURRENT, not historical. Ownership answers "whose is
this"; it does not answer "may they see it now".

That distinction matters because stored material is not inert. An answer
headline can embed a WAC amount ("Gross revenue: $250,766,926.42") or a
territory label, and the conversation title is the question text. Filtering by
owner alone meant a user who had since lost pricing permission, or been moved
to another territory, could still read both back through the history and list
endpoints -- with the list leaking titles even where a body was withheld.

So every path -- open, list, load, write -- requires the conversation's
recorded scope fingerprint to still equal the principal's current one. The
fingerprint covers user, role, scope value and pricing permission, so losing
WAC or changing territory makes prior material inaccessible immediately, with
no separate revocation step. Continuation additionally requires the dataset and
semantic contract versions to match, because a refresh makes a carried plan
incomparable rather than merely old.

Retention is deliberately not deletion: the rows remain for an administrator or
audit path under a different policy. What changes is that the ordinary
endpoints stop returning them.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from app.auth.policy import Principal
from app.db import auth_transaction


class ConversationAccessError(PermissionError):
    pass


@dataclass
class ConversationState:
    conversation_id: str
    previous_plan: dict[str, Any] | None = None
    previous_cohort: list[str] = field(default_factory=list)
    # The dimension those ids are FOR. A cohort without its grain is just a
    # list of strings, and was being reapplied as account ids whatever it
    # actually held.
    previous_cohort_dimension: str | None = None
    #: False when the stored cohort was truncated at the cap. A turn recorded
    #: before this was tracked reports False: unknown is not complete.
    previous_cohort_complete: bool = True
    previous_cohort_total: int | None = None
    next_seq: int = 1
    reset_reason: str | None = None
    #: The conversation revision this request read. A turn commits only if it
    #: is unchanged -- see finalise().
    revision: int = 0
    #: A clarification the previous turn asked and nobody has answered yet.
    pending_clarification: "PendingClarification | None" = None


@dataclass(frozen=True)
class PendingClarification:
    clarification_id: str
    kind: str
    question: str
    slot_text: str | None
    #: As shown, in the order shown: [{"id", "label", "detail"}, ...]
    choices: list[dict[str, str]]
    dataset_id: str | None
    #: The paused workflow thread that asked. Resumed only through this row,
    #: which open_conversation reached through the owner check.
    graph_thread_id: str | None = None


@dataclass
class StagedTurn:
    """What a request will commit, assembled on its way through and written
    in ONE transaction by finalise(). Before this, each exit path wrote its
    own turn, best-effort, and a failure was logged and swallowed while the
    response said "answered"."""
    question: str
    plan: dict[str, Any] | None
    answer_text: str
    status: str
    cohort_dimension: str | None = None
    cohort_ids: list[str] = field(default_factory=list)
    cohort_complete: bool = True
    cohort_total: int | None = None
    #: A clarification this turn is asking, to be answered by the next.
    clarification: dict[str, Any] | None = None
    #: The pending clarification this turn answered.
    resolves_clarification: str | None = None


@dataclass(frozen=True)
class Finalised:
    persisted: bool
    conflict: bool = False
    turn_seq: int | None = None
    reason: str | None = None


def _new_id() -> str:
    return "c_" + secrets.token_urlsafe(12)


# One SQL predicate, used by every read and write, so the four paths cannot
# drift apart. Parameter order is (conversation_id?, owner_user_id, fingerprint).
_CURRENT_AUTH = "owner_user_id = %s AND scope_fingerprint = %s"


def open_conversation(
    principal: Principal,
    conversation_id: str | None,
    *,
    dataset_id: str | None = None,
    metric_version: str | None = None,
    policy_version: str | None = None,
) -> ConversationState:
    """Open an existing conversation or start a new one.

    Continuation requires the scope fingerprint AND the dataset and semantic
    contract versions to still match. A refresh or a contract bump makes a
    carried plan incomparable, not merely stale.
    """
    fingerprint = principal.fingerprint()

    with auth_transaction() as cur:
        if conversation_id:
            # Owner check and scope check in one statement.
            cur.execute(
                "SELECT conversation_id, owner_user_id, scope_fingerprint, "
                "       dataset_id, metric_version, policy_version, revision "
                "FROM app_conv.conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ConversationAccessError("That conversation does not exist.")
            if row["owner_user_id"] != principal.user_id:
                # Deliberately the same message as 'not found': whether another
                # user's conversation exists is not disclosed.
                raise ConversationAccessError("That conversation does not exist.")

            incompatible = None
            if row["scope_fingerprint"] != fingerprint:
                incompatible = (
                    "Your access level changed, so this conversation started fresh "
                    "rather than reusing earlier results."
                )
            elif dataset_id and row["dataset_id"] and row["dataset_id"] != dataset_id:
                incompatible = (
                    "The underlying data was refreshed, so this conversation started "
                    "fresh rather than comparing against the previous snapshot."
                )
            elif (metric_version and row["metric_version"]
                  and row["metric_version"] != metric_version) or (
                  policy_version and row["policy_version"]
                  and row["policy_version"] != policy_version):
                incompatible = (
                    "The metric or policy definitions changed, so this conversation "
                    "started fresh rather than reusing an incomparable plan."
                )

            if incompatible:
                # Do not carry the cohort, the plan, or anything derived from
                # them across an authorization or semantic boundary.
                new_id = _new_id()
                cur.execute(
                    "INSERT INTO app_conv.conversations (conversation_id, owner_user_id, "
                    "scope_fingerprint, dataset_id, metric_version, policy_version) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (new_id, principal.user_id, fingerprint, dataset_id,
                     metric_version, policy_version),
                )
                return ConversationState(conversation_id=new_id, reset_reason=incompatible)

            cur.execute(
                "SELECT plan, resolved_cohort, cohort_dimension, cohort_complete, "
                "       cohort_total, cohort_id, seq FROM app_conv.turns "
                "WHERE conversation_id = %s AND status = 'answered' "
                "ORDER BY seq DESC LIMIT 1",
                (conversation_id,),
            )
            last = cur.fetchone()

            # The full, distinct population, from the cohort tables. Turns
            # recorded before they existed fall back to the 200-id column,
            # whose completeness is then read conservatively below.
            members: list[str] | None = None
            if last and last["cohort_id"]:
                cur.execute(
                    "SELECT entity_id FROM app_conv.cohort_members "
                    "WHERE cohort_id = %s ORDER BY ordinal", (last["cohort_id"],))
                members = [r["entity_id"] for r in cur.fetchall()]

            cur.execute(
                "SELECT clarification_id, kind, question, slot_text, choices, dataset_id, "
                "       graph_thread_id "
                "FROM app_conv.clarifications "
                "WHERE conversation_id = %s AND status = 'pending' AND expires_at > now() "
                "  AND scope_fingerprint = %s",
                (conversation_id, fingerprint))
            pending_row = cur.fetchone()
            pending = None
            if pending_row and (not dataset_id or pending_row["dataset_id"] in (None, dataset_id)):
                pending = PendingClarification(
                    clarification_id=pending_row["clarification_id"],
                    kind=pending_row["kind"], question=pending_row["question"],
                    slot_text=pending_row["slot_text"],
                    choices=list(pending_row["choices"] or []),
                    dataset_id=pending_row["dataset_id"],
                    graph_thread_id=pending_row["graph_thread_id"])
            cur.execute(
                "SELECT COALESCE(max(seq), 0) AS m FROM app_conv.turns "
                "WHERE conversation_id = %s",
                (conversation_id,),
            )
            next_seq = cur.fetchone()["m"] + 1

            return ConversationState(
                conversation_id=conversation_id,
                revision=int(row["revision"] or 0),
                pending_clarification=pending,
                previous_plan=last["plan"] if last else None,
                previous_cohort=(members if members is not None
                                 else list(last["resolved_cohort"] or []) if last else []),
                previous_cohort_dimension=last["cohort_dimension"] if last else None,
                # NULL means the turn predates the column. Treated as
                # incomplete, because unknown completeness is not completeness.
                previous_cohort_complete=(
                    bool(last["cohort_complete"]) if last and last["cohort_complete"] is not None
                    else not bool(last and last["resolved_cohort"])),
                previous_cohort_total=last["cohort_total"] if last else None,
                next_seq=next_seq,
            )

        new_id = _new_id()
        cur.execute(
            "INSERT INTO app_conv.conversations (conversation_id, owner_user_id, "
            "scope_fingerprint, dataset_id, metric_version, policy_version) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (new_id, principal.user_id, fingerprint, dataset_id,
             metric_version, policy_version),
        )
        return ConversationState(conversation_id=new_id)


def record_turn(
    principal: Principal,
    state: ConversationState,
    *,
    question: str,
    plan: dict[str, Any] | None,
    cohort: list[str],
    answer_text: str,
    status: str,
    cohort_dimension: str | None = None,
    cohort_complete: bool = True,
    cohort_total: int | None = None,
) -> None:
    with auth_transaction() as cur:
        # Serialise writers on this conversation for the rest of the
        # transaction. Sequence numbers were computed by reading max(seq) in
        # one request and inserting in another, so two concurrent turns picked
        # the same number; the insert said ON CONFLICT DO NOTHING and one of
        # them disappeared. The user saw their answer and the conversation had
        # no record of the question.
        #
        # A lock rather than a retry loop: retries race too, and the contended
        # case here is two turns in the same thread of conversation, which is
        # rare and short.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (state.conversation_id,))

        # Writing is authorized by the same current rule as reading: a turn
        # must not be appended to a thread the principal could no longer open.
        cur.execute(
            f"SELECT 1 FROM app_conv.conversations "
            f"WHERE conversation_id = %s AND {_CURRENT_AUTH}",
            (state.conversation_id, principal.user_id, principal.fingerprint()),
        )
        if cur.fetchone() is None:
            raise ConversationAccessError("That conversation does not exist.")

        # Decided under the lock, so it cannot collide.
        cur.execute(
            "SELECT COALESCE(max(seq), 0) + 1 AS seq FROM app_conv.turns "
            "WHERE conversation_id = %s",
            (state.conversation_id,),
        )
        seq = cur.fetchone()["seq"]

        cur.execute(
            """
            INSERT INTO app_conv.turns
                (conversation_id, seq, question, plan, resolved_cohort,
                 cohort_dimension, cohort_complete, cohort_total,
                 answer_text, status)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
            """,
            (
                state.conversation_id, seq, question[:2000],
                json.dumps(plan, default=str) if plan else None,
                json.dumps(cohort), cohort_dimension,
                cohort_complete, cohort_total,
                answer_text[:8000], status,
            ),
        )
        state.next_seq = seq + 1

        cur.execute(
            "UPDATE app_conv.conversations SET updated_at = now(), "
            "revision = revision + 1, "
            "title = COALESCE(title, %s) WHERE conversation_id = %s",
            (question[:120], state.conversation_id),
        )


#: How long a clarification waits for its answer.
CLARIFICATION_TTL_SECONDS = 1800


def finalise(principal: Principal, state: ConversationState, run, turn: StagedTurn,
             outcome: dict[str, Any] | None, *,
             audit: Callable[[Any], None] | None = None) -> Finalised:
    """Commit a request's turn and outcome as ONE fact.

    In one transaction: the turn, its full cohort, the clarification it asks
    or answers, the conversation's revision, and the run's outcome. Either
    all of it is recorded or none of it is, and the caller is told which --
    the previous version logged a failed write and returned "answered" as
    though the next turn could rely on it.

    The revision check is what makes an overlapping request safe without
    holding a transaction across planning: if another turn committed after
    this request read the conversation, this one is refused rather than
    recorded as though it had planned against the latest state.

    `audit`, when given, writes the request's audit row in this same
    transaction (strict audit, docs/AUDIT_DECISION.md); if it fails, nothing
    here commits.
    """
    import secrets as _secrets

    try:
        with auth_transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (state.conversation_id,))
            cur.execute(
                f"SELECT revision FROM app_conv.conversations "
                f"WHERE conversation_id = %s AND {_CURRENT_AUTH} FOR UPDATE",
                (state.conversation_id, principal.user_id, principal.fingerprint()))
            row = cur.fetchone()
            if row is None:
                return Finalised(persisted=False, reason="access")

            if run is not None and int(row["revision"]) != run.base_revision:
                cur.execute(
                    "UPDATE app_conv.runs SET status = 'conflicted', finished_at = now() "
                    "WHERE run_id = %s", (run.run_id,))
                return Finalised(persisted=False, conflict=True, reason="revision")

            cur.execute("SELECT COALESCE(max(seq), 0) + 1 AS seq FROM app_conv.turns "
                        "WHERE conversation_id = %s", (state.conversation_id,))
            seq = cur.fetchone()["seq"]

            cohort_id = None
            if turn.cohort_dimension and turn.cohort_ids:
                cohort_id = "k_" + _secrets.token_urlsafe(12)
                cur.execute(
                    "INSERT INTO app_conv.cohorts (cohort_id, conversation_id, turn_seq, "
                    "  dimension, member_count, complete, dataset_id, scope_fingerprint) "
                    "SELECT %s, %s, %s, %s, %s, %s, dataset_id, %s "
                    "FROM app_conv.conversations WHERE conversation_id = %s",
                    (cohort_id, state.conversation_id, seq, turn.cohort_dimension,
                     len(turn.cohort_ids), turn.cohort_complete, principal.fingerprint(),
                     state.conversation_id))
                with cur.copy("COPY app_conv.cohort_members (cohort_id, ordinal, entity_id) "
                              "FROM STDIN") as copy:
                    for ordinal, entity_id in enumerate(turn.cohort_ids):
                        copy.write_row((cohort_id, ordinal, entity_id))

            cur.execute(
                """
                INSERT INTO app_conv.turns
                    (conversation_id, seq, question, plan, resolved_cohort,
                     cohort_dimension, cohort_complete, cohort_total, cohort_id,
                     run_id, answer_text, status)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                """,
                (state.conversation_id, seq, turn.question[:2000],
                 json.dumps(turn.plan, default=str) if turn.plan else None,
                 # The legacy column keeps its old bound for old readers; the
                 # full population lives in cohort_members.
                 json.dumps(turn.cohort_ids[:200]), turn.cohort_dimension,
                 turn.cohort_complete, turn.cohort_total, cohort_id,
                 run.run_id if run is not None else None,
                 turn.answer_text[:8000], turn.status))

            # A pending clarification ends here one way or another: answered
            # by this turn, or superseded by a different question.
            cur.execute(
                "UPDATE app_conv.clarifications SET status = %s "
                "WHERE conversation_id = %s AND status = 'pending'",
                ("resolved" if turn.resolves_clarification else "superseded",
                 state.conversation_id))
            if turn.clarification:
                c = turn.clarification
                cur.execute(
                    "INSERT INTO app_conv.clarifications (clarification_id, conversation_id, "
                    "  turn_seq, kind, question, slot_text, choices, dataset_id, "
                    "  scope_fingerprint, expires_at, graph_thread_id) "
                    "SELECT %s, %s, %s, %s, %s, %s, %s::jsonb, dataset_id, %s, "
                    "  now() + make_interval(secs => %s), %s "
                    "FROM app_conv.conversations WHERE conversation_id = %s",
                    ("q_" + _secrets.token_urlsafe(12), state.conversation_id, seq,
                     c["kind"], c["question"][:2000], c.get("slot_text"),
                     json.dumps(c["choices"]), principal.fingerprint(),
                     CLARIFICATION_TTL_SECONDS, c.get("graph_thread_id"),
                     state.conversation_id))

            cur.execute(
                "UPDATE app_conv.conversations SET updated_at = now(), "
                "revision = revision + 1, title = COALESCE(title, %s) "
                "WHERE conversation_id = %s",
                (turn.question[:120], state.conversation_id))

            if audit is not None:
                audit(cur)

            if run is not None:
                cur.execute(
                    "UPDATE app_conv.runs SET status = 'succeeded', outcome = %s::jsonb, "
                    "  turn_seq = %s, finished_at = now() WHERE run_id = %s",
                    (json.dumps(outcome, default=str) if outcome else None, seq, run.run_id))

        state.next_seq = seq + 1
        state.revision = int(row["revision"]) + 1
        return Finalised(persisted=True, turn_seq=seq)
    except ConversationAccessError:
        return Finalised(persisted=False, reason="access")


def list_conversations(principal: Principal, limit: int = 25) -> list[dict[str, Any]]:
    with auth_transaction() as cur:
        # The title is the question text, which is itself user content, so the
        # whole row is withheld rather than blanked -- a redacted entry would
        # still disclose that a conversation exists under a scope the principal
        # no longer holds.
        cur.execute(
            f"SELECT conversation_id, title, updated_at FROM app_conv.conversations "
            f"WHERE {_CURRENT_AUTH} ORDER BY updated_at DESC LIMIT %s",
            (principal.user_id, principal.fingerprint(), limit),
        )
        return [dict(r) for r in cur.fetchall()]


def load_history(principal: Principal, conversation_id: str) -> list[dict[str, Any]]:
    with auth_transaction() as cur:
        cur.execute(
            """
            SELECT t.seq, t.question, t.answer_text, t.status, t.created_at
            FROM app_conv.turns t
            JOIN app_conv.conversations c ON c.conversation_id = t.conversation_id
            WHERE t.conversation_id = %s
              AND c.owner_user_id = %s
              AND c.scope_fingerprint = %s
            ORDER BY t.seq
            """,
            (conversation_id, principal.user_id, principal.fingerprint()),
        )
        return [dict(r) for r in cur.fetchall()]
