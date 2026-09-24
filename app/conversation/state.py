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
    next_seq: int = 1
    reset_reason: str | None = None


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
                "       dataset_id, metric_version, policy_version "
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
                "SELECT plan, resolved_cohort, cohort_dimension, seq FROM app_conv.turns "
                "WHERE conversation_id = %s AND status = 'answered' "
                "ORDER BY seq DESC LIMIT 1",
                (conversation_id,),
            )
            last = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(max(seq), 0) AS m FROM app_conv.turns "
                "WHERE conversation_id = %s",
                (conversation_id,),
            )
            next_seq = cur.fetchone()["m"] + 1

            return ConversationState(
                conversation_id=conversation_id,
                previous_plan=last["plan"] if last else None,
                previous_cohort=list(last["resolved_cohort"] or []) if last else [],
                previous_cohort_dimension=last["cohort_dimension"] if last else None,
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
                 cohort_dimension, answer_text, status)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            """,
            (
                state.conversation_id, seq, question[:2000],
                json.dumps(plan, default=str) if plan else None,
                json.dumps(cohort), cohort_dimension,
                answer_text[:8000], status,
            ),
        )
        state.next_seq = seq + 1

        cur.execute(
            "UPDATE app_conv.conversations SET updated_at = now(), "
            "title = COALESCE(title, %s) WHERE conversation_id = %s",
            (question[:120], state.conversation_id),
        )


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
