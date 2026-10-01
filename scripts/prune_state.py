#!/usr/bin/env python3
"""Prune workflow state that can no longer be resumed.

    python3 scripts/prune_state.py [--orphan-hours 24]

Safe to run on a schedule: it never touches a thread whose clarification is
still pending, and a checkpoint is workflow state, not user history.
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
    args = ap.parse_args()
    result = prune(checkpointer(), orphan_hours=args.orphan_hours)
    print(f"expired {result.clarifications_expired} clarification(s), deleted "
          f"{result.threads_deleted} thread(s) and {result.runs_deleted} run record(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
