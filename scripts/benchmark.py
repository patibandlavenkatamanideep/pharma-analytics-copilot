#!/usr/bin/env python3
"""Measure query latency at full scale, alone and under concurrency.

  python3 scripts/benchmark.py                 # single-threaded
  python3 scripts/benchmark.py --concurrency 8 --iterations 5

Uses the deterministic offline planner, so the numbers are database plus
compile plus render and exclude model latency. That is deliberate: model
latency is a separate, provider-dependent cost, and mixing the two would hide
which layer is slow.

Reports p50 and p95 per question shape rather than a single average, because an
average hides the tail that actually determines whether a request times out.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Question shapes chosen to span the cost range: a scalar aggregate, a wide
# grouping, a two-source ratio, a two-window comparison, and the most expensive
# shape the system can produce.
SHAPES = [
    ("scalar total", "exec", "What were our pack units last month?"),
    ("product grouping", "exec", "Show me pack units by product this quarter"),
    ("top-N accounts", "exec", "What are the top 10 accounts by pack units this quarter?"),
    ("market share", "exec", "What is our market share for Zenovax?"),
    ("share by territory", "exec", "Show me Zenovax market share by territory"),
    ("growth comparison", "exec",
     "Which accounts grew the most versus the prior quarter?"),
    ("revenue by product", "exec", "What is our revenue by product last month?"),
    ("scoped top-N", "ram", "What are my top 10 accounts by pack units this quarter?"),
    ("scoped share", "ram", "Show me Zenovax market share for my territory"),
    ("region breakdown", "director",
     "How are the territories in my region performing this quarter?"),
]


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round((pct / 100) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.auth.policy import principal_for_user_id
    from app.db import close_pools, owner_transaction
    from app.llm.planner import OfflinePlanner
    from app.pipeline import Pipeline

    pipeline = Pipeline(OfflinePlanner())
    dataset = pipeline.current_dataset()

    with owner_transaction() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (u.role) u.user_id, u.email, u.role
            FROM users u
            ORDER BY u.role,
                     CASE WHEN u.role = 'exec' THEN 1
                          WHEN u.role = 'director' AND EXISTS (
                               SELECT 1 FROM zip_territory z
                               WHERE z.region_name = u.region_name) THEN 1
                          WHEN u.role = 'ram' AND EXISTS (
                               SELECT 1 FROM zip_territory z
                               WHERE z.territory_name = u.territory_name) THEN 1
                          ELSE 2 END, u.user_id
            """
        )
        rows = cur.fetchall()

    # Built directly from the users table -- no password is minted, so running
    # this never disturbs credentials that have been issued to anyone.
    principals = {row["role"]: principal_for_user_id(row["user_id"]) for row in rows}

    print(f"dataset {dataset['dataset_id']} "
          f"({dataset['row_counts'].get('sales', 0):,} sales rows)")
    print(f"concurrency {args.concurrency}, {args.iterations} iterations per shape\n")

    def run_once(item):
        label, role, question = item
        started = time.perf_counter()
        result = pipeline.ask(principals[role], question)
        return label, (time.perf_counter() - started) * 1000, result.status

    work = [shape for shape in SHAPES for _ in range(args.iterations)]
    timings: dict[str, list[float]] = {label: [] for label, _, _ in SHAPES}
    errors: dict[str, int] = {}

    wall_start = time.perf_counter()
    if args.concurrency > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for label, ms, status in pool.map(run_once, work):
                timings[label].append(ms)
                if status != "answered":
                    errors[label] = errors.get(label, 0) + 1
    else:
        for item in work:
            label, ms, status = run_once(item)
            timings[label].append(ms)
            if status != "answered":
                errors[label] = errors.get(label, 0) + 1
    wall = time.perf_counter() - wall_start

    print(f"  {'shape':<22} {'n':>3} {'p50':>9} {'p95':>9} {'max':>9}  errors")
    print(f"  {'-' * 22} {'-' * 3} {'-' * 9} {'-' * 9} {'-' * 9}  ------")
    report = {}
    for label, _, _ in SHAPES:
        samples = timings[label]
        p50, p95 = percentile(samples, 50), percentile(samples, 95)
        report[label] = {
            "n": len(samples), "p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
            "max_ms": round(max(samples), 1), "errors": errors.get(label, 0),
        }
        print(f"  {label:<22} {len(samples):>3} {p50:>8.0f}m {p95:>8.0f}m "
              f"{max(samples):>8.0f}m  {errors.get(label, 0)}")

    everything = [ms for samples in timings.values() for ms in samples]
    print(f"\n  overall p50 {percentile(everything, 50):.0f} ms, "
          f"p95 {percentile(everything, 95):.0f} ms, "
          f"max {max(everything):.0f} ms over {len(everything)} requests")
    print(f"  {len(everything) / wall:.1f} requests/second at concurrency {args.concurrency}")
    if sum(errors.values()):
        print(f"  {sum(errors.values())} non-answered responses: {errors}")

    if args.json:
        print(json.dumps({
            "dataset": dataset["dataset_id"],
            "concurrency": args.concurrency,
            "overall_p50_ms": round(percentile(everything, 50), 1),
            "overall_p95_ms": round(percentile(everything, 95), 1),
            "requests_per_second": round(len(everything) / wall, 2),
            "shapes": report,
        }, indent=2))

    close_pools()
    return 0


if __name__ == "__main__":
    sys.exit(main())
