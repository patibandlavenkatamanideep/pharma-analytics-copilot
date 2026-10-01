#!/usr/bin/env python3
"""Build the disposable ingestion-test database.

Ingestion tests add, correct and delete sales and advance the reporting
anchor. That must never happen to the working database, and the
authorization tests depend on the disposable auth database staying as
seeded -- so ingestion gets a database of its own.

  python3 scripts/build_ingesttest_db.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = os.environ.get("PAC_INGESTTEST_DB", "pharma_analytics_ingesttest")


def main() -> int:
    # Nothing reads a database being built, so nothing to wait for before
    # reclaiming what the load replaced.
    env = {**os.environ, "PAC_DB_NAME": DB, "PYTHONPATH": str(ROOT),
           "PAC_PUBLICATION_SETTLE_SECONDS": "0"}
    for argv, label in (([str(ROOT / "scripts" / "bootstrap_db.py"), "--drop", "--no-env"],
                         "bootstrapped"),
                        ([str(ROOT / "scripts" / "load_data.py"), "--mode", "seed"],
                         "seed data loaded into")):
        r = subprocess.run([sys.executable, *argv], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:], file=sys.stderr)
            return r.returncode
        print(f"  {label} {DB}")
    print(f"\n{DB} ready (disposable; safe to drop)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
