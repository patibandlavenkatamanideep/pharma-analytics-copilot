#!/usr/bin/env python3
"""Readiness report for the dataset published in one database.

    python3 scripts/readiness_report.py --db pac_profile_orchard \
        [--onboarding <onboarding-report.json>] [--real-data yes|no] --out <report.json>

Reads the published manifest and the database (app/data/readiness.py) and
writes, section by section, what was measured and its status: ready,
attention, blocked or not_measured. Read-only. `--onboarding` adds a
build_profile_db.py reconciliation; `--real-data` records whether the data is
a real feed (omitted: unknown). Exits 0 unless a section is blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--onboarding", type=pathlib.Path, default=None)
    ap.add_argument("--real-data", choices=("yes", "no"), default=None)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    os.environ["PAC_DB_NAME"] = args.db

    from app.data.readiness import build
    from app.db import close_pools, owner_transaction

    onboarding = json.loads(args.onboarding.read_text()) if args.onboarding else None
    real = None if args.real_data is None else args.real_data == "yes"
    if onboarding is not None and real is None:
        real = onboarding.get("real_data")
    with owner_transaction() as cur:
        report = build(cur, real_data=real, onboarding=onboarding)
    close_pools()
    args.out.write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(json.dumps({"overall": report["overall"],
                      **{k: v["status"] for k, v in report["sections"].items()}}))
    return 1 if report["overall"] == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
