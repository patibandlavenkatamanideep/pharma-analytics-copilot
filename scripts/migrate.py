#!/usr/bin/env python3
"""Apply schema migrations and set up the graph store, as the OWNER role.

For deployments: roles are created once by bootstrap_db.py (which needs a
superuser); every later schema change is applied by this, which needs only
the owner. It runs in the one-shot `jobs` container -- the serving container
does not hold the owner credential at all.

    python3 scripts/migrate.py

Every migration is written to be re-runnable, so this converges.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    import psycopg

    from app.config import get_settings

    settings = get_settings()
    if not settings.db_owner_password and settings.environment == "cloud":
        print("migrate: PAC_DB_OWNER_PASSWORD is not set; run this in the jobs container",
              file=sys.stderr)
        return 2
    with psycopg.connect(settings.dsn("owner"), autocommit=True) as conn, conn.cursor() as cur:
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            cur.execute(path.read_text())
            print(f"applied {path.name}")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "setup_graph_store.py")],
                   check=True, env=dict(os.environ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
