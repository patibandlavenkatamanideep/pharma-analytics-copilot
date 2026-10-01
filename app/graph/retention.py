"""Pruning workflow state that can no longer be resumed.

A finished turn prunes its own thread at once. What is left behind:

* a clarification that expired, or was superseded by a different question
  -- its paused thread can never be resumed;
* a run that died part-way and was never retried -- its thread waits for a
  retry that will not come once its idempotency key has expired;
* run rows past their idempotency retention.

Checkpoints are workflow state, not history: the history is the turn in
app_conv. Deleting them loses nothing a user can see.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db import auth_transaction


@dataclass(frozen=True)
class Pruned:
    clarifications_expired: int
    threads_deleted: int
    runs_deleted: int


def prune(checkpointer, *, orphan_hours: int = 24) -> Pruned:
    with auth_transaction() as cur:
        cur.execute(
            "UPDATE app_conv.clarifications SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at <= now()")
        expired = cur.rowcount

        # Threads a clarification named, once it can no longer be answered.
        cur.execute(
            "SELECT DISTINCT graph_thread_id AS t FROM app_conv.clarifications "
            "WHERE graph_thread_id IS NOT NULL AND status <> 'pending'")
        threads = {r["t"] for r in cur.fetchall()}

        # Threads of runs that finished, or died, long enough ago that no
        # retry can resume them: <conversation>.<run>.
        cur.execute(
            "SELECT conversation_id || '.' || run_id AS t FROM app_conv.runs "
            "WHERE (status <> 'running' OR lease_expires_at <= now()) "
            "  AND created_at <= now() - make_interval(hours => %s)", (orphan_hours,))
        threads |= {r["t"] for r in cur.fetchall()}

        cur.execute("DELETE FROM app_conv.runs WHERE expires_at <= now() AND status <> 'running'")
        runs_deleted = cur.rowcount

    # Paused threads whose clarification is still pending are never touched.
    with auth_transaction() as cur:
        cur.execute("SELECT graph_thread_id AS t FROM app_conv.clarifications "
                    "WHERE status = 'pending' AND graph_thread_id IS NOT NULL")
        threads -= {r["t"] for r in cur.fetchall()}

    for thread_id in threads:
        checkpointer.delete_thread(thread_id)
    return Pruned(expired, len(threads), runs_deleted)
