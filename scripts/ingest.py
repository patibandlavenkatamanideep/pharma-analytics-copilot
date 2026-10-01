#!/usr/bin/env python3
"""Apply incremental batches to the published dataset, as the OWNER role.

Runs in the one-shot `jobs` container, like migrations and full loads: the
serving container holds no owner credential.

    python3 scripts/ingest.py batch1.json batch2.json      # JSON batch files
    python3 scripts/ingest.py --synthetic [--seed 7]       # the synthetic feed

Each batch is reconciled, validated and published (or rejected) on its own;
the outcome of each is printed as one JSON line. Exit status is 1 if any
batch was rejected.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, time, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _synthetic(seed: int):
    from app.data.sources import SyntheticIncrementalSource
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute("SELECT org_id FROM organizations WHERE org_status = 'Active' ORDER BY 1")
        orgs = [r["org_id"] for r in cur.fetchall()]
        cur.execute("SELECT ndc FROM products ORDER BY 1")
        ndcs = [r["ndc"] for r in cur.fetchall()]
        cur.execute("SELECT max(week_ending_date) AS we FROM app_ref.calendar")
        latest = datetime.combine(datetime.fromisoformat(cur.fetchone()["we"]).date(),
                                  time(12), tzinfo=timezone.utc)
    return SyntheticIncrementalSource(orgs=orgs, ndcs=ndcs, latest_week_ending=latest, seed=seed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=pathlib.Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if bool(args.files) == args.synthetic:
        parser.error("give batch files, or --synthetic")

    from app.data.ingest import ingest
    from app.data.sources import JsonBatchFiles
    from app.db import close_pools

    source = _synthetic(args.seed) if args.synthetic else JsonBatchFiles(args.files)
    rejected = False
    try:
        for batch in source.batches():
            outcome = ingest(batch)
            rejected |= outcome.status == "rejected"
            print(json.dumps(outcome.as_dict(), default=str))
    finally:
        close_pools()
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
