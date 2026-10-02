#!/usr/bin/env python3
"""Apply incremental batches to the published dataset, as the OWNER role.

Runs in the one-shot `jobs` container, like migrations and full loads: the
serving container holds no owner credential.

    python3 scripts/ingest.py batch1.json batch2.json      # JSON batch files
    python3 scripts/ingest.py --synthetic [--seed 7]       # the synthetic feed
    python3 scripts/ingest.py --check-freshness --max-since-success 26h \\
        [--max-watermark-age 3d] [--source distributor-feed]

Each batch is reconciled, validated and published (or rejected) on its own;
the outcome of each is printed as one JSON line. Exit status is 1 if any
batch was rejected.

`--check-freshness` ingests nothing. It reads how long ago each source last
delivered an accepted batch, and how old its newest event is, prints them as
JSON and exits 3 if any limit is exceeded or an expected source never
delivered. A scheduler can run it independently of ingestion and of any
collector (app/data/freshness.py).

Telemetry: this is a separate process from the API, so it configures its
own exporters (PAC_OTEL_ENDPOINT) and flushes them on the way out, within a
bounded time. A collector that is down or hangs never fails or holds up
ingestion: publication commits before the flush, and the flush gives up
after twice the exporter timeout.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import datetime, time, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("ingest")


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


def _start_telemetry() -> None:
    """Export what this process measures, if a collector is configured.
    Never fatal: ingestion does not depend on telemetry."""
    from app import telemetry
    from app.config import get_settings

    try:
        telemetry.configure(get_settings())
    except Exception as exc:
        log.warning("telemetry not started (%s); ingesting without it", type(exc).__name__)


def _check_freshness(args) -> int:
    from app.data import freshness

    found = freshness.read()
    problems = freshness.problems(found, expected=args.source,
                                  max_since_success=args.max_since_success,
                                  max_watermark_age=args.max_watermark_age)
    print(json.dumps({"sources": [f.as_dict() for f in found], "problems": problems}))
    return 3 if problems else 0


def main() -> int:
    from app.data.freshness import duration

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=pathlib.Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--check-freshness", action="store_true",
                        help="report source freshness and exit 3 if stale; ingests nothing")
    parser.add_argument("--max-since-success", type=duration, default=None,
                        help="longest acceptable time since a source's last accepted batch")
    parser.add_argument("--max-watermark-age", type=duration, default=None,
                        help="oldest acceptable newest-event time")
    parser.add_argument("--source", action="append", default=[],
                        help="a source that must have delivered (repeatable)")
    args = parser.parse_args()
    if args.check_freshness:
        if args.files or args.synthetic:
            parser.error("--check-freshness ingests nothing; give no batches")
    elif bool(args.files) == args.synthetic:
        parser.error("give batch files, or --synthetic")

    from app import logs, telemetry
    from app.db import close_pools

    logs.configure()
    _start_telemetry()
    try:
        if args.check_freshness:
            return _check_freshness(args)

        from app.data.ingest import ingest
        from app.data.sources import JsonBatchFiles

        source = _synthetic(args.seed) if args.synthetic else JsonBatchFiles(args.files)
        rejected = False
        for batch in source.batches():
            outcome = ingest(batch)
            rejected |= outcome.status == "rejected"
            print(json.dumps(outcome.as_dict(), default=str))
        return 1 if rejected else 0
    finally:
        # Telemetry first: its last collection reads freshness through the
        # pools. Bounded, so a dead collector cannot hold the job open.
        telemetry.shutdown()
        close_pools()


if __name__ == "__main__":
    sys.exit(main())
