#!/usr/bin/env python3
"""Create the graph checkpoint tables, as the migration role.

LangGraph's PostgresSaver.setup() is the supported way to create and migrate
its tables. It creates tables and indexes, so it runs as the OWNER role --
the same role that applies migrations -- and never inside the serving
process, which holds DML privileges on these tables and nothing more.

Run after the SQL migrations (bootstrap_db.py calls it):

    python3 scripts/setup_graph_store.py

Idempotent: setup() records its own migration version.
"""

from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row


def main() -> int:
    from app.config import get_settings
    from langgraph.checkpoint.postgres import PostgresSaver

    settings = get_settings()
    # bootstrap_db.py has just generated the owner's password and passes the
    # DSN to this child only, in its environment; standalone runs use the
    # configured owner credentials.
    dsn = os.environ.get("PAC_SETUP_OWNER_DSN") or settings.dsn("owner")
    # search_path pins the checkpointer's unqualified table names to
    # app_graph. autocommit because setup() creates indexes CONCURRENTLY,
    # which cannot run inside a transaction block.
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=0,
                         row_factory=dict_row,
                         options="-c search_path=app_graph") as conn:
        PostgresSaver(conn).setup()
        with conn.cursor() as cur:
            # Default privileges cover tables created after 013; this covers
            # any that existed before it, so a rerun converges.
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE "
                        "ON ALL TABLES IN SCHEMA app_graph TO pac_auth")
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'app_graph' ORDER BY 1")
            tables = [r["table_name"] for r in cur.fetchall()]
    print(f"graph store ready in {settings.db_name}.app_graph: {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
