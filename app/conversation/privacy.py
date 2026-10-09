"""A user's own data: export it, delete it.

Owner-scoped. Every statement is keyed by the caller's user id, which comes
from the session and never from the request, so no request can name another
user's data.

What a user owns, and where it lives:

=====================  ====================================================
conversations, turns   app_conv -- their questions and the answers shown
cohorts, members       app_conv -- the populations a "those accounts" refers to
feedback               app_conv -- ratings and comments on answers
clarifications         app_conv -- questions asked back, and their choices
runs                   app_conv -- request records, with the stored outcome
                       an idempotent retry replays
checkpoints            app_graph -- workflow state, one thread per run
sessions, identities   app_auth -- sign-in state; not deleted here (signing
                       out is POST /api/logout)
audit rows             app_meta.query_audit -- the security record; see below
=====================  ====================================================

**Export** is bound to CURRENT access, like every other read. A
conversation recorded under access the user no longer holds is withheld
whole -- its questions can name accounts from a territory they have left --
and counted, so the export does not pretend it is complete.

**Deletion** covers everything the user owns, whatever access it was
recorded under: deleting discloses nothing. A conversation with a request
still running is refused rather than deleted from under it.

**Audit exception.** Audit rows are not deleted on request. They hold
hashes, codes, counts and timings -- no question text, SQL or results -- and
they are the record of what was accessed, which a deletion request must not
be able to erase. The serving role cannot delete them at all; they expire
under the audit retention period (app/retention.py), run by the jobs
container.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.auth.policy import Principal
from app.db import auth_transaction


class ConversationBusy(RuntimeError):
    """A request in this conversation is still running."""


@dataclass(frozen=True)
class Deleted:
    conversations: int
    threads: int


def _threads(cur: Any, conversation_ids: list[str]) -> list[str]:
    """Checkpoint threads belonging to these conversations: <conversation>.<run>."""
    if not conversation_ids:
        return []
    cur.execute(
        "SELECT DISTINCT thread_id FROM app_graph.checkpoints "
        "WHERE split_part(thread_id, '.', 1) = ANY(%s)", (conversation_ids,))
    return [r["thread_id"] for r in cur.fetchall()]


def _delete(principal: Principal, conversation_ids: list[str] | None,
            checkpointer) -> Deleted:
    with auth_transaction() as cur:
        clause, params = "owner_user_id = %s", [principal.user_id]
        if conversation_ids is not None:
            clause += " AND conversation_id = ANY(%s)"
            params.append(conversation_ids)
        cur.execute(f"SELECT conversation_id FROM app_conv.conversations WHERE {clause} "
                    f"FOR UPDATE", params)
        owned = [r["conversation_id"] for r in cur.fetchall()]
        if not owned:
            return Deleted(0, 0)
        cur.execute(
            "SELECT 1 FROM app_conv.runs WHERE conversation_id = ANY(%s) "
            "AND status = 'running' AND lease_expires_at > now() LIMIT 1", (owned,))
        if cur.fetchone():
            raise ConversationBusy("a question in this conversation is still being answered")
        threads = _threads(cur, owned)
        # Turns, cohorts and their members, clarifications and runs go with
        # the conversation (ON DELETE CASCADE).
        cur.execute("DELETE FROM app_conv.conversations WHERE conversation_id = ANY(%s)",
                    (owned,))
        deleted = cur.rowcount
    # Workflow state last: if this fails, what remains is unreachable --
    # its conversation is gone -- and the orphan pruning removes it.
    for thread_id in threads:
        checkpointer.delete_thread(thread_id)
    return Deleted(deleted, len(threads))


def delete_conversation(principal: Principal, conversation_id: str, checkpointer) -> bool:
    """Delete one of the caller's conversations. False when there is no such
    conversation of theirs -- the same answer for someone else's."""
    return _delete(principal, [conversation_id], checkpointer).conversations == 1


def delete_all(principal: Principal, checkpointer) -> Deleted:
    """Delete every conversation the caller owns, and its workflow state."""
    return _delete(principal, None, checkpointer)


def export(principal: Principal) -> dict[str, Any]:
    """Everything the caller may currently see of their own data."""
    fingerprint = principal.fingerprint()
    with auth_transaction() as cur:
        cur.execute(
            "SELECT conversation_id, title, created_at, updated_at, scope_fingerprint "
            "FROM app_conv.conversations WHERE owner_user_id = %s ORDER BY created_at",
            (principal.user_id,))
        rows = cur.fetchall()
        visible = [r for r in rows if r["scope_fingerprint"] == fingerprint]
        withheld = len(rows) - len(visible)
        ids = [r["conversation_id"] for r in visible]

        cur.execute(
            "SELECT conversation_id, seq, question, answer_text, status, created_at, "
            "       cohort_dimension, cohort_complete, cohort_total "
            "FROM app_conv.turns WHERE conversation_id = ANY(%s) ORDER BY conversation_id, seq",
            (ids,))
        turns: dict[str, list[dict[str, Any]]] = {}
        for t in cur.fetchall():
            turns.setdefault(t.pop("conversation_id"), []).append(dict(t))

        cur.execute(
            "SELECT c.conversation_id, c.turn_seq, c.dimension, c.member_count, c.complete, "
            "       array_agg(m.entity_id ORDER BY m.ordinal) AS members "
            "FROM app_conv.cohorts c LEFT JOIN app_conv.cohort_members m USING (cohort_id) "
            "WHERE c.conversation_id = ANY(%s) GROUP BY 1, 2, 3, 4, 5, c.cohort_id",
            (ids,))
        cohorts: dict[str, list[dict[str, Any]]] = {}
        for c in cur.fetchall():
            cohorts.setdefault(c.pop("conversation_id"), []).append(dict(c))

        cur.execute(
            "SELECT conversation_id, turn_seq, kind, question, status, created_at, expires_at "
            "FROM app_conv.clarifications WHERE conversation_id = ANY(%s) ORDER BY created_at",
            (ids,))
        clarifications: dict[str, list[dict[str, Any]]] = {}
        for c in cur.fetchall():
            clarifications.setdefault(c.pop("conversation_id"), []).append(dict(c))

        cur.execute(
            "SELECT conversation_id, turn_seq, rating, reason, comment, created_at "
            "FROM app_conv.feedback WHERE conversation_id = ANY(%s) "
            "ORDER BY conversation_id, turn_seq", (ids,))
        feedback: dict[str, list[dict[str, Any]]] = {}
        for f in cur.fetchall():
            feedback.setdefault(f.pop("conversation_id"), []).append(dict(f))

        cur.execute(
            "SELECT conversation_id, run_id, status, turn_seq, created_at, finished_at "
            "FROM app_conv.runs WHERE conversation_id = ANY(%s) ORDER BY created_at", (ids,))
        runs: dict[str, list[dict[str, Any]]] = {}
        for r in cur.fetchall():
            runs.setdefault(r.pop("conversation_id"), []).append(dict(r))

        cur.execute(
            "SELECT created_at, last_seen_at, expires_at, revoked_at, user_agent "
            "FROM app_auth.sessions WHERE user_id = %s ORDER BY created_at",
            (principal.user_id,))
        sessions = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT issuer, subject, created_at FROM app_auth.identities "
                    "WHERE user_id = %s ORDER BY created_at", (principal.user_id,))
        identities = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(*) AS n, min(created_at) AS first, max(created_at) AS last "
                    "FROM app_meta.query_audit WHERE user_id = %s", (principal.user_id,))
        audit = dict(cur.fetchone())

    return {
        "user": {"name": principal.full_name, "email": principal.email,
                 "role": principal.role, "scope": principal.scope_description},
        "conversations": [
            {"conversation_id": r["conversation_id"], "title": r["title"],
             "created_at": r["created_at"], "updated_at": r["updated_at"],
             "turns": turns.get(r["conversation_id"], []),
             "cohorts": cohorts.get(r["conversation_id"], []),
             "clarifications": clarifications.get(r["conversation_id"], []),
             "runs": runs.get(r["conversation_id"], []),
             "feedback": feedback.get(r["conversation_id"], [])}
            for r in visible],
        "withheld_conversations": withheld,
        "withheld_reason": (
            "recorded under access you no longer hold; deleting your data removes them too"
            if withheld else None),
        "sessions": sessions,
        "identities": identities,
        "audit_records": {
            "count": audit["n"], "first": audit["first"], "last": audit["last"],
            "note": ("security audit records hold hashes, codes and timings, not your "
                     "questions or results; they are kept for the audit retention "
                     "period and are not removed by a deletion request"),
        },
    }
