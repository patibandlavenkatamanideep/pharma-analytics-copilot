#!/usr/bin/env python3
"""A redacted sample of recent failures, for triage. Run as the owner, in
the jobs container.

    python3 scripts/failure_sample.py [--days 7] [--limit 50] [--include-comments]

Draws from two sources: requests that ended in an error, a refusal by a
blocking intent gap, or a refresh (the audit trail), and answers their
askers marked as not right (feedback). Each record carries what is needed to
reproduce the failure with a synthetic question -- outcome, reason codes,
intent gaps, metric and plan hashes, versions, timings, role -- and nothing
that identifies the user or quotes them: no user id, no email, no question
text, no answer. Feedback comments are the user's own words and are left
out unless --include-comments is given.

The way back into the product is a regression test, written by a person,
that reproduces the failure with an invented question. Never the user's
text, and never a policy relaxed to make a complaint go away.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = ("error", "planner_error", "planner_unavailable", "db_error", "compile_error",
          "validation_error", "grain_error", "deadline_exceeded", "generation_changed",
          "unsupported_combination")


def sample(days: int, limit: int, include_comments: bool) -> dict:
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute(
            "SELECT request_id, created_at, role, status, reason_codes, intent_gaps, "
            "       metric_version, policy_version, prompt_version, model_id, plan_hash, "
            "       sql_hash, row_count, db_ms, total_ms, planner_attempts, dataset_id "
            "FROM app_meta.query_audit "
            "WHERE created_at > now() - make_interval(days => %s) "
            "  AND (status = ANY(%s) OR status = 'clarify' AND reason_codes && %s) "
            "ORDER BY random() LIMIT %s",
            (days, list(FAILED), ["ranking_direction_mismatch", "dropped_product",
                                  "dropped_account", "invented_product", "invented_account"],
             limit))
        failures = [dict(r) for r in cur.fetchall()]
        cur.execute(
            # Not joined to runs: they are pruned after the idempotency period,
            # and a complaint outlives it.
            "SELECT f.created_at, f.reason, f.turn_seq, f.comment, "
            "       t.plan->>'metric' AS metric, t.status AS turn_status "
            "FROM app_conv.feedback f "
            "LEFT JOIN app_conv.turns t ON t.conversation_id = f.conversation_id "
            "                         AND t.seq = f.turn_seq "
            "WHERE f.rating = -1 AND f.created_at > now() - make_interval(days => %s) "
            "ORDER BY random() LIMIT %s", (days, limit))
        complaints = [dict(r) for r in cur.fetchall()]
    if not include_comments:
        for c in complaints:
            c["comment"] = "[withheld]" if c["comment"] else None
    return {"days": days, "failures": failures, "marked_not_right": complaints}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--include-comments", action="store_true")
    args = ap.parse_args()
    print(json.dumps(sample(args.days, args.limit, args.include_comments), default=str,
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
