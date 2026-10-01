#!/usr/bin/env python3
"""Prune workflow state that can no longer be resumed, then apply retention.

    python3 scripts/prune_state.py [--orphan-hours 24] [--no-retention]

Safe to run on a schedule: it never touches a thread whose clarification is
still pending or a conversation with a request in flight. Retention runs as
the owner (the audit trail is append-only for the serving role), so this
belongs in the jobs container. Periods are in docs/RETENTION.md.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from app.graph import checkpointer
    from app.graph.retention import prune

    ap = argparse.ArgumentParser()
    ap.add_argument("--orphan-hours", type=int, default=24)
    ap.add_argument("--no-retention", action="store_true",
                    help="only prune workflow state; skip the retention periods")
    args = ap.parse_args()
    saver = checkpointer()
    result = prune(saver, orphan_hours=args.orphan_hours)
    print(f"expired {result.clarifications_expired} clarification(s), deleted "
          f"{result.threads_deleted} thread(s) and {result.runs_deleted} run record(s)")
    if not args.no_retention:
        from app import retention

        kept = retention.apply(checkpointer=saver)
        print("retention: " + ", ".join(f"{n} {k}" for k, n in kept.deleted.items())
              + f", {len(kept.threads)} thread(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
