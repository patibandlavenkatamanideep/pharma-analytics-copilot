#!/usr/bin/env python3
"""Measure incremental ingestion at full scale, on a DISPOSABLE copy.

Applies three batches and samples reader latency throughout: 500 sales in the
latest week, the same batch replayed, then 500 sales in a new week (which
rewrites every fact's week offset). Prints one JSON document.

Never point this at a database anyone depends on -- it publishes new
generations. It refuses the configured working database name outright:

  createdb -T pharma_analytics -O pac_owner pharma_analytics_ingestscale
  PAC_DB_NAME=pharma_analytics_ingestscale python3 scripts/measure_ingestion.py
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROTECTED = {"pharma_analytics"}
READER_SQL = ("SELECT mo_offset, sum(pack_units) AS v FROM sales "
              "WHERE data_source = 'distributor' AND brand_flag = 1 "
              "AND mo_offset IN (0, 1, 2) GROUP BY 1")


def main() -> int:
    from app.config import get_settings
    from app.data.ingest import ingest
    from app.data.sources import SyntheticIncrementalSource
    from app.db import analytics_transaction, close_pools, owner_transaction

    db = get_settings().db_name
    if db in PROTECTED:
        print(f"refusing to publish into {db!r}; use a disposable copy", file=sys.stderr)
        return 2

    with owner_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM sales")
        rows = cur.fetchone()["n"]
        cur.execute("SELECT org_id FROM organizations WHERE org_status = 'Active' "
                    "ORDER BY 1 LIMIT 500")
        orgs = [r["org_id"] for r in cur.fetchall()]
        cur.execute("SELECT ndc FROM products ORDER BY 1")
        ndcs = [r["ndc"] for r in cur.fetchall()]
        cur.execute("SELECT max(week_ending_date) AS we FROM app_ref.calendar")
        latest = datetime.fromisoformat(cur.fetchone()["we"]).replace(
            hour=16, tzinfo=timezone.utc)
    now = latest + timedelta(days=11)

    def read_once() -> float:
        start = time.perf_counter()
        with analytics_transaction(scope_kind="global", scope_value=None,
                                   wac_authorized=False) as cur:
            cur.execute(READER_SQL)
            cur.fetchall()
        return (time.perf_counter() - start) * 1000

    idle = [read_once() for _ in range(10)]

    results: dict = {"database": db, "sales_rows": rows, "batches": [],
                     "reader_ms": {"idle": _summary(idle)}}

    def timed(label: str, batch, sample_readers: bool = False) -> None:
        samples: list[float] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                samples.append(read_once())
                stop.wait(1.0)

        thread = threading.Thread(target=reader) if sample_readers else None
        if thread:
            thread.start()
        start = time.perf_counter()
        out = ingest(batch, now=now)
        elapsed = time.perf_counter() - start
        stop.set()
        if thread:
            thread.join()
            results["reader_ms"]["during_" + label] = _summary(samples)
        results["batches"].append({
            "label": label, "seconds": round(elapsed, 2), "status": out.status,
            "applied": out.applied, "duplicates": out.duplicates,
            "anchor_shift_weeks": out.anchor_shift_weeks})

    in_week = SyntheticIncrementalSource(orgs=orgs, ndcs=ndcs, latest_week_ending=latest,
                                         seed=11).new_sales("scale-in-week", 500)
    timed("in_latest_week", in_week, sample_readers=True)
    timed("replayed", in_week)
    new_week = SyntheticIncrementalSource(orgs=orgs, ndcs=ndcs,
                                          latest_week_ending=latest + timedelta(days=7),
                                          seed=12).new_sales("scale-new-week", 500)
    timed("new_week", new_week, sample_readers=True)
    close_pools()
    print(json.dumps(results))
    return 0


def _summary(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    return {"n": len(samples), "min": round(min(samples), 1),
            "median": round(statistics.median(samples), 1), "max": round(max(samples), 1)}


if __name__ == "__main__":
    sys.exit(main())
