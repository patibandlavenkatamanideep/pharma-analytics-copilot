#!/usr/bin/env python3
"""Backup and restore drill: dump a database, restore it into a DISPOSABLE
one, and prove the restored copy serves the same answers under the same
security boundary. Prints one JSON document with the measured times.

    python3 scripts/restore_drill.py [--source pharma_analytics] \\
        [--target pharma_analytics_restoretest]

pg_dump only reads the source (one consistent snapshot, no blocking of
readers or writers). The target must be named pharma_analytics_restore*;
it is dropped and recreated. Runs with the local PostgreSQL superuser for
createdb and restore, as an operator would; the application roles are
cluster-wide and already exist here. Restoring into a NEW cluster needs
scripts/bootstrap_db.py (roles) first -- see docs/RUNBOOK.md.

RTO here is restore-to-ready: restore, then every check below, then the
application reporting ready. The dump's duration is the cost of taking a
backup, not of recovering from one. RPO is set by how often backups are
taken, which this script cannot measure; see docs/RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TABLES = ["sales", "organizations", "products", "zip_territory", "users",
          "app_ref.calendar", "app_ref.product_classification", "app_meta.dataset_manifest",
          "app_meta.query_audit", "app_conv.conversations", "app_conv.turns",
          "app_auth.credentials", "app_ingest.event_ledger"]
QUESTIONS = ["total paid pack units last quarter",
             "top 10 accounts by paid pack units last quarter",
             "market share by territory last quarter"]


def run(argv: list[str]) -> float:
    started = time.perf_counter()
    subprocess.run(argv, check=True, capture_output=True, text=True)
    return round(time.perf_counter() - started, 2)


def facts(db: str) -> dict:
    import psycopg
    with psycopg.connect(dbname=db) as conn:
        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
        generation = conn.execute("SELECT dataset_id FROM app_ref.generation").fetchone()[0]
        policies = conn.execute("SELECT count(*) FROM pg_policies").fetchone()[0]
        grants = conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE grantee LIKE 'pac_%'").fetchone()[0]
        column_grants = conn.execute(
            "SELECT count(*) FROM information_schema.column_privileges "
            "WHERE grantee LIKE 'pac_%'").fetchone()[0]
    return {"counts": counts, "generation": generation, "policies": policies,
            "table_grants": grants, "column_grants": column_grants}


def answers(db: str) -> dict:
    """The same questions, as a scoped user and an executive, through the
    real pipeline in a child process pointed at `db`."""
    code = r"""
import json, sys
from app.auth.policy import principal_for_user_id
from app.db import owner_transaction, verify_runtime_role_safety
from app.llm.planner import OfflinePlanner
from app.pipeline import Pipeline
problems = verify_runtime_role_safety()
with owner_transaction() as cur:
    cur.execute("SELECT DISTINCT ON (role) user_id, role FROM users WHERE "
                "role = 'exec' OR (role = 'ram' AND territory_name IN "
                "(SELECT territory_name FROM zip_territory)) ORDER BY role, user_id")
    users = {r["role"]: r["user_id"] for r in cur.fetchall()}
pipe = Pipeline(OfflinePlanner())
ready = pipe.current_dataset()["dataset_id"]
out = {}
for role, uid in users.items():
    p = principal_for_user_id(uid)
    for q in json.loads(sys.argv[1]):
        r = pipe.ask(p, q)
        out[f"{role}: {q}"] = [r.status, r.answer.headline if r.answer else r.message,
                               r.answer.row_count if r.answer else None]
print(json.dumps({"boundary_problems": problems, "ready_dataset": ready, "answers": out}))
"""
    env = {**os.environ, "PAC_DB_NAME": db, "PAC_LLM_PROVIDER": "offline",
           "PYTHONPATH": str(ROOT), "PAC_USER_REQUESTS_PER_MINUTE": "100000",
           "PAC_USER_REQUESTS_PER_HOUR": "1000000"}
    r = subprocess.run([sys.executable, "-c", code, json.dumps(QUESTIONS)], cwd=ROOT, env=env,
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.environ.get("PAC_DB_NAME", "pharma_analytics"))
    ap.add_argument("--target", default="pharma_analytics_restoretest")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    if not args.target.startswith("pharma_analytics_restore"):
        print("the target must be a disposable pharma_analytics_restore* database",
              file=sys.stderr)
        return 2
    if args.target == args.source:
        print("source and target are the same database", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        dump = pathlib.Path(tmp) / "pac.dump"
        before = facts(args.source)
        dump_s = run(["pg_dump", "-Fc", "-f", str(dump), args.source])
        size_mb = round(dump.stat().st_size / 1e6, 1)
        # The source may have moved on since the dump; compare the restore
        # with the source as it was when the dump began.
        run(["dropdb", "--if-exists", args.target])
        started = time.perf_counter()
        run(["createdb", "-O", "pac_owner", args.target])
        restore_s = run(["pg_restore", "-d", args.target, "-j", str(args.jobs), str(dump)])
        after = facts(args.target)
        served = answers(args.target)
        rto_s = round(time.perf_counter() - started, 2)
    reference = answers(args.source)

    checks = {
        "row_counts_match": before["counts"] == after["counts"],
        "generation_matches": before["generation"] == after["generation"],
        "policies_match": before["policies"] == after["policies"],
        "grants_match": (before["table_grants"], before["column_grants"])
                        == (after["table_grants"], after["column_grants"]),
        "boundary_intact": served["boundary_problems"] == [],
        "ready": served["ready_dataset"] == before["generation"],
        "answers_match": served["answers"] == reference["answers"],
    }
    print(json.dumps({
        "source": args.source, "target": args.target, "dump_seconds": dump_s,
        "dump_mb": size_mb, "restore_seconds": restore_s,
        "restore_to_ready_seconds": rto_s, "checks": checks,
        "sales_rows": after["counts"]["sales"], "answers": served["answers"],
    }, default=str))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
