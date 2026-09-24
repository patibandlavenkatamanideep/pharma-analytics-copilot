#!/usr/bin/env python3
"""Load business data.

  python3 scripts/load_data.py --mode seed
  python3 scripts/load_data.py --mode full

The two modes are mutually exclusive: each truncates the other's rows before
loading, because the seed fixture and the generated CSVs reuse the same
reference keys.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from app.data.loader import LoadError, load
from app.db import close_pools


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("seed", "full"), required=True)
    ap.add_argument("--json", action="store_true", help="print the manifest as JSON")
    args = ap.parse_args()

    started = time.time()
    try:
        report = load(args.mode)
    except LoadError as exc:
        print(f"LOAD FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        pass
    elapsed = time.time() - started

    if args.json:
        print(json.dumps(report.__dict__, indent=2, default=str))
    else:
        print(f"dataset  {report.dataset_id}  ({args.mode}, published in {elapsed:.1f}s)")
        print("\nrow counts")
        for table, count in sorted(report.row_counts.items()):
            print(f"  {table:<16} {count:>10,}")
        print("\nreporting anchor")
        for key, value in report.reporting_anchor.items():
            print(f"  {key:<24} {value}")
        print("\nsource coverage")
        for key, value in report.source_coverage.items():
            print(f"  {key:<32} {value}")
        print(f"\ndata-quality warnings ({len(report.warnings)})")
        for warning in report.warnings:
            print(f"  [{warning['code']}]")
            print(f"     {warning['message']}")
    close_pools()
    return 0


if __name__ == "__main__":
    sys.exit(main())
