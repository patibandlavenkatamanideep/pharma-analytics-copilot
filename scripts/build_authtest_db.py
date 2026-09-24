#!/usr/bin/env python3
"""Build the disposable authorization-test database.

Separate from the working database on purpose: these tests mutate `users` to
simulate a permission change (losing WAC, changing role, moving territory), and
that must never happen to real data. The working database is never dropped,
truncated or edited by the test suite.

  python3 scripts/build_authtest_db.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = os.environ.get("PAC_AUTHTEST_DB", "pharma_analytics_authtest")


def main() -> int:
    env = {**os.environ, "PAC_DB_NAME": DB, "PYTHONPATH": str(ROOT)}
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"), "--drop", "--no-env"],
        env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:], file=sys.stderr)
        return r.returncode
    print(f"  bootstrapped {DB}")

    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "load_data.py"), "--mode", "seed"],
        env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:], file=sys.stderr)
        return r.returncode
    print(f"  seed data loaded into {DB}")
    print(f"\n{DB} ready (disposable; safe to drop)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
