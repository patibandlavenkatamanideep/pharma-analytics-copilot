#!/usr/bin/env python3
"""Build the separate coherent-market fixture database.

Kept apart from the application database on purpose: the fixture models a
market the supplied data cannot (one where market_data includes the company's
own volume), and mixing the two would make it impossible to tell which dataset
a result came from.

  python3 scripts/build_fixture_db.py
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE_DB = os.environ.get("PAC_FIXTURE_DB", "pharma_analytics_fixture")


def main() -> int:
    os.environ["PAC_DB_NAME"] = FIXTURE_DB
    sys.argv = [sys.argv[0]]          # bootstrap parses its own args

    import subprocess

    env = {**os.environ, "PAC_DB_NAME": FIXTURE_DB, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"), "--drop", "--no-env"],
        env=env, capture_output=True, text=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode

    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    get_settings.cache_clear()
    sql = (ROOT / "tests" / "fixtures" / "coherent_market.sql").read_text()
    with owner_transaction() as cur:
        cur.execute(sql)
        cur.execute("ANALYZE sales")
        for table in ("organizations", "products", "sales", "zip_territory"):
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            print(f"  {table:<16} {cur.fetchone()['n']:>4}")
    close_pools()
    print(f"\nfixture database '{FIXTURE_DB}' ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
