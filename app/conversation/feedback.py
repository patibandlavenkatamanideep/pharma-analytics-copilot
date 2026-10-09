"""Feedback on an answer, from the person who asked for it.

Permission-controlled the same way as reading the answer: the caller must
own the run, and the run must have been recorded under the access they hold
now. Feedback on someone else's answer, or on one recorded under access
since withdrawn, is the same "not found" as feedback on nothing.

Feedback does not change behaviour by itself. It is a signal for triage
(scripts/failure_sample.py); a confirmed failure becomes a regression test
written by a person, with a synthetic question -- not the user's text --
and policy is never relaxed to make a complaint go away.
"""

from __future__ import annotations

from app.auth.policy import Principal
from app.conversation.runs import run_status
from app.db import auth_transaction

REASONS = ("wrong_number", "different_question", "missing_data", "too_slow", "other")


def record(principal: Principal, run_id: str, *, helpful: bool, reason: str | None = None,
           comment: str | None = None) -> bool:
    """Record (or replace) feedback on one answered turn. False when there is
    no such answered turn of the caller's under their current access."""
    if reason is not None and reason not in REASONS:
        raise ValueError(f"reason must be one of {REASONS}")
    run = run_status(principal, run_id)
    if run is None or run["status"] != "succeeded" or run["turn_seq"] is None:
        return False
    with auth_transaction() as cur:
        cur.execute(
            "INSERT INTO app_conv.feedback (conversation_id, turn_seq, owner_user_id, run_id, "
            " rating, reason, comment) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (conversation_id, turn_seq) DO UPDATE SET rating = EXCLUDED.rating, "
            " reason = EXCLUDED.reason, comment = EXCLUDED.comment, created_at = now() "
            "WHERE app_conv.feedback.owner_user_id = EXCLUDED.owner_user_id",
            (run["conversation_id"], run["turn_seq"], principal.user_id, run_id,
             1 if helpful else -1, None if helpful else reason,
             (comment or "").strip()[:500] or None))
    return True
