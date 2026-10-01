"""Scheduled retention: delete what has outlived its purpose.

Run by the jobs container as the OWNER (scripts/prune_state.py), because one
of its stores -- the audit trail -- is append-only for the serving role, and
should stay that way.

Every period is configuration. The defaults are proposals, not an agreed
policy; docs/RETENTION.md says so and lists them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import get_settings
from app.db import owner_transaction


@dataclass
class Retained:
    deleted: dict[str, int] = field(default_factory=dict)
    threads: list[str] = field(default_factory=list)


def apply(settings=None, checkpointer=None) -> Retained:
    """One pass. Idempotent; safe to run on any schedule. With a
    checkpointer, the deleted conversations' workflow threads go too."""
    s = settings or get_settings()
    out = Retained()
    with owner_transaction() as cur:
        # Conversations untouched for the retention period, with everything
        # hanging off them (cascade). Never one with a live request.
        cur.execute(
            "SELECT conversation_id FROM app_conv.conversations c "
            "WHERE updated_at < now() - make_interval(days => %s) "
            "  AND NOT EXISTS (SELECT 1 FROM app_conv.runs r "
            "                  WHERE r.conversation_id = c.conversation_id "
            "                    AND r.status = 'running' AND r.lease_expires_at > now()) "
            "FOR UPDATE SKIP LOCKED", (s.conversation_retention_days,))
        stale = [r["conversation_id"] for r in cur.fetchall()]
        if stale:
            cur.execute(
                "SELECT DISTINCT thread_id FROM app_graph.checkpoints "
                "WHERE split_part(thread_id, '.', 1) = ANY(%s)", (stale,))
            out.threads = [r["thread_id"] for r in cur.fetchall()]
            cur.execute("DELETE FROM app_conv.conversations WHERE conversation_id = ANY(%s)",
                        (stale,))
        out.deleted["conversations"] = len(stale)

        for name, sql, days in (
            ("sessions",
             "DELETE FROM app_auth.sessions WHERE COALESCE(revoked_at, expires_at) "
             "< now() - make_interval(days => %s)", s.session_record_retention_days),
            ("login_attempts",
             "DELETE FROM app_auth.login_attempts "
             "WHERE attempted_at < now() - make_interval(days => %s)",
             s.login_attempt_retention_days),
            ("audit_rows",
             "DELETE FROM app_meta.query_audit "
             "WHERE created_at < now() - make_interval(days => %s)", s.audit_retention_days),
            ("quarantined_events",
             "DELETE FROM app_ingest.quarantine "
             "WHERE quarantined_at < now() - make_interval(days => %s)",
             s.quarantine_retention_days),
        ):
            cur.execute(sql, (days,))
            out.deleted[name] = cur.rowcount
        cur.execute("DELETE FROM app_auth.oidc_pending WHERE expires_at < now()")
        out.deleted["oidc_pending"] = cur.rowcount
    # After the commit: a thread whose conversation is gone is unreachable,
    # so a failure here leaves only what the orphan pruning removes.
    if checkpointer is not None:
        for thread_id in out.threads:
            checkpointer.delete_thread(thread_id)
    return out
