"""Owner-scoped conversation state.

What is persisted is the structured plan and the resolved entity ids, never SQL
and never result rows. A follow-up therefore patches a typed object rather than
re-parsing old prose, and old text can never overwrite who the user is.

Every read is filtered by owner_user_id: knowing a conversation id is not
enough to open it. A conversation also records the scope it was created under,
so if the owner's role or assignment changes, carried-over state is discarded
rather than silently reused under different permissions.
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
    next_seq: int = 1
    reset_reason: str | None = None


def _new_id() -> str:
    return "c_" + secrets.token_urlsafe(12)


def open_conversation(
    principal: Principal, conversation_id: str | None
) -> ConversationState:
    """Open an existing conversation or start a new one."""
    fingerprint = principal.fingerprint()

    with auth_transaction() as cur:
        if conversation_id:
            # Owner check and scope check in one statement.
            cur.execute(
                "SELECT conversation_id, owner_user_id, scope_fingerprint "
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

            if row["scope_fingerprint"] != fingerprint:
                # Access changed since this thread started. Start clean rather
                # than carry a cohort the user may no longer be allowed to see.
                new_id = _new_id()
                cur.execute(
                    "INSERT INTO app_conv.conversations "
                    "(conversation_id, owner_user_id, scope_fingerprint) VALUES (%s, %s, %s)",
                    (new_id, principal.user_id, fingerprint),
                )
                return ConversationState(
                    conversation_id=new_id,
                    reset_reason=(
                        "Your access level changed, so this conversation started fresh "
                        "rather than reusing earlier results."
                    ),
                )

            cur.execute(
                "SELECT plan, resolved_cohort, seq FROM app_conv.turns "
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
                next_seq=next_seq,
            )

        new_id = _new_id()
        cur.execute(
            "INSERT INTO app_conv.conversations "
            "(conversation_id, owner_user_id, scope_fingerprint) VALUES (%s, %s, %s)",
            (new_id, principal.user_id, fingerprint),
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
) -> None:
    with auth_transaction() as cur:
        cur.execute(
            "SELECT 1 FROM app_conv.conversations "
            "WHERE conversation_id = %s AND owner_user_id = %s",
            (state.conversation_id, principal.user_id),
        )
        if cur.fetchone() is None:
            raise ConversationAccessError("That conversation does not exist.")

        cur.execute(
            """
            INSERT INTO app_conv.turns
                (conversation_id, seq, question, plan, resolved_cohort, answer_text, status)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            ON CONFLICT (conversation_id, seq) DO NOTHING
            """,
            (
                state.conversation_id, state.next_seq, question[:2000],
                json.dumps(plan, default=str) if plan else None,
                json.dumps(cohort),
                answer_text[:8000], status,
            ),
        )
        cur.execute(
            "UPDATE app_conv.conversations SET updated_at = now(), "
            "title = COALESCE(title, %s) WHERE conversation_id = %s",
            (question[:120], state.conversation_id),
        )


def list_conversations(principal: Principal, limit: int = 25) -> list[dict[str, Any]]:
    with auth_transaction() as cur:
        cur.execute(
            "SELECT conversation_id, title, updated_at FROM app_conv.conversations "
            "WHERE owner_user_id = %s ORDER BY updated_at DESC LIMIT %s",
            (principal.user_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def load_history(principal: Principal, conversation_id: str) -> list[dict[str, Any]]:
    with auth_transaction() as cur:
        cur.execute(
            """
            SELECT t.seq, t.question, t.answer_text, t.status, t.created_at
            FROM app_conv.turns t
            JOIN app_conv.conversations c ON c.conversation_id = t.conversation_id
            WHERE t.conversation_id = %s AND c.owner_user_id = %s
            ORDER BY t.seq
            """,
            (conversation_id, principal.user_id),
        )
        return [dict(r) for r in cur.fetchall()]
