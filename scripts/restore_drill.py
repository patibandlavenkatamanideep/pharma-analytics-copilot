#!/usr/bin/env python3
"""Backup and restore drill: dump a database, restore it into a DISPOSABLE
one, and prove the restored copy serves the same answers under the same
security boundary. Prints one JSON document with the measured times.

    python3 scripts/restore_drill.py [--source pharma_analytics] \\
        [--target pharma_analytics_restoretest]
    python3 scripts/restore_drill.py --new-cluster [--source pharma_analytics] \\
        [--target pharma_analytics_restored]

pg_dump only reads the source (one consistent snapshot, no blocking of
readers or writers). The target must be named pharma_analytics_restore*.

Same cluster (the default): the target is dropped and recreated in the local
cluster, where the application roles already exist. Runs with the local
PostgreSQL superuser for createdb and restore, as an operator would.

New cluster (--new-cluster): the procedure in docs/RUNBOOK.md §8 for a lost
host. A disposable copy of the source (<target>_src, in the local cluster)
is first given application state -- an answer committed under an
idempotency key and a clarification paused mid-conversation -- and its data
cutoff is read: the newest audit row, turn, run and published dataset. The
copy is dumped; a new cluster is created (initdb in a temporary directory,
its own port, a superuser password generated here); the roles are created
and the dump restored as RUNBOOK §8 says. Then the same-cluster checks, plus
the role memberships, the restored cutoff equal to the copy's, the stored
answer replayed and the paused clarification resumed in the new cluster.
Every step is timed; the cluster and the copy are removed at the end.

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
import secrets
import shutil
import socket
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
#: Application state beyond the business data.
STATE_TABLES = ["app_conv.runs", "app_conv.run_attempts", "app_conv.clarifications",
                "app_auth.sessions", "app_graph.checkpoints", "app_ingest.batches"]
QUESTIONS = ["total paid pack units last quarter",
             "top 10 accounts by paid pack units last quarter",
             "market share by territory last quarter"]
TWIN = "Restore Drill Twin Facility"
PASSWORD_KEYS = ("OWNER", "AUTH", "EXEC", "SCOPED")


def run(argv: list[str], env: dict | None = None) -> float:
    started = time.perf_counter()
    subprocess.run(argv, check=True, capture_output=True, text=True, env=env)
    return round(time.perf_counter() - started, 2)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    """Where a database lives: the local cluster (no arguments; libpq
    defaults and the repository's .env), or a new one on its own port."""

    def __init__(self, port: int | None = None, admin: str | None = None,
                 admin_pw: str | None = None, passwords: dict[str, str] | None = None):
        self.port, self.admin, self.admin_pw = port, admin, admin_pw
        self.passwords = passwords or {}

    def connect(self, db: str):
        import psycopg
        if self.port is None:
            return psycopg.connect(dbname=db, autocommit=True)
        return psycopg.connect(dbname=db, host="127.0.0.1", port=self.port, user=self.admin,
                               password=self.admin_pw, autocommit=True)

    def tool_env(self) -> dict[str, str]:
        """For createdb, pg_restore and friends."""
        if self.port is None:
            return dict(os.environ)
        return {**os.environ, "PGHOST": "127.0.0.1", "PGPORT": str(self.port),
                "PGUSER": self.admin, "PGPASSWORD": self.admin_pw}

    def app_env(self, db: str) -> dict[str, str]:
        """For a child running the application against `db`."""
        env = {**os.environ, "PAC_DB_NAME": db, "PAC_LLM_PROVIDER": "offline",
               "PYTHONPATH": str(ROOT), "PAC_USER_REQUESTS_PER_MINUTE": "100000",
               "PAC_USER_REQUESTS_PER_HOUR": "1000000", "AWS_EC2_METADATA_DISABLED": "true"}
        if self.port is not None:
            env.update({"PAC_DB_HOST": "127.0.0.1", "PAC_DB_PORT": str(self.port),
                        **{f"PAC_DB_{k}_PASSWORD": v for k, v in self.passwords.items()}})
        return env

    def admin_dsn(self, db: str = "postgres") -> str:
        from psycopg.conninfo import make_conninfo
        return make_conninfo(host="127.0.0.1", port=self.port, dbname=db, user=self.admin,
                             password=self.admin_pw)


LOCAL = Cluster()


def facts(db: str, cluster: Cluster = LOCAL) -> dict:
    with cluster.connect(db) as conn:
        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in TABLES + STATE_TABLES}
        generation = conn.execute("SELECT dataset_id FROM app_ref.generation").fetchone()[0]
        policies = conn.execute("SELECT count(*) FROM pg_policies").fetchone()[0]
        rls = conn.execute("SELECT count(*) FILTER (WHERE relrowsecurity), "
                           "count(*) FILTER (WHERE relforcerowsecurity) FROM pg_class").fetchone()
        # The ACLs themselves, from the catalog: information_schema shows only
        # privileges involving roles the connecting user belongs to, so the
        # same grants read differently to the local superuser and to a new
        # cluster's.
        grants = conn.execute(
            "SELECT count(*), md5(coalesce(string_agg(n.nspname || '.' || c.relname || '='"
            " || c.relacl::text, ',' ORDER BY n.nspname, c.relname), '')) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relacl IS NOT NULL "
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema')").fetchone()
        column_grants = conn.execute(
            "SELECT count(*), md5(coalesce(string_agg(c.oid::regclass::text || '.' || a.attname"
            " || '=' || a.attacl::text, ',' ORDER BY c.oid::regclass::text, a.attname), '')) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE a.attacl IS NOT NULL "
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema')").fetchone()
        memberships = conn.execute(
            "SELECT m.rolname || ' -> ' || r.rolname FROM pg_auth_members am "
            "JOIN pg_roles r ON r.oid = am.roleid JOIN pg_roles m ON m.oid = am.member "
            "WHERE m.rolname LIKE 'pac_%' ORDER BY 1").fetchall()
        connect = conn.execute(
            "SELECT array_agg(r ORDER BY r) FROM unnest(ARRAY['pac_auth_login', "
            "'pac_exec_login', 'pac_scoped_login']) r "
            "WHERE has_database_privilege(r, current_database(), 'CONNECT')").fetchone()[0]
    return {"counts": counts, "generation": generation, "policies": policies,
            "rls_tables": list(rls), "table_grants": list(grants),
            "column_grants": list(column_grants),
            "memberships": [m[0] for m in memberships], "connect": connect or []}


def cutoff(db: str, cluster: Cluster = LOCAL) -> dict:
    """The newest committed fact of each kind: what a restore brings back."""
    with cluster.connect(db) as conn:
        row = conn.execute(
            "SELECT (SELECT max(created_at) FROM app_meta.query_audit), "
            "       (SELECT max(created_at) FROM app_conv.turns), "
            "       (SELECT max(created_at) FROM app_conv.runs), "
            "       (SELECT max(published_at) FROM app_meta.dataset_manifest), "
            "       (SELECT max(transaction_date) FROM sales)").fetchone()
    return dict(zip(("audit", "turn", "run", "dataset_published", "sales_through"),
                    [str(v) if v is not None else None for v in row]))


def child(code: str, env: dict, *args: str) -> dict:
    r = subprocess.run([sys.executable, "-c", code, *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


ANSWERS = r"""
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

#: In the source copy: an answer committed under a key, and a clarification
#: left waiting for its reply. Server-side tooling (principal_for_user_id).
MAKE_STATE = r"""
import json, secrets, sys
from app.auth.policy import principal_for_user_id
from app.db import owner_transaction
from app.llm.planner import OfflinePlanner
from app.pipeline import Pipeline
twin = sys.argv[1]
with owner_transaction() as cur:
    cur.execute("SELECT DISTINCT ON (zip) state, zip FROM organizations WHERE zip IS NOT NULL "
                "ORDER BY zip LIMIT 2")
    places = cur.fetchall()
    for i, p in enumerate(places):
        cur.execute("INSERT INTO organizations (org_id, org_name, org_type, org_status, state, "
                    "zip) VALUES (%s, %s, 'Facility', 'Active', %s, %s)",
                    (f"RESTORE-TW{i}-{secrets.token_hex(3)}", twin, p["state"], p["zip"]))
    cur.execute("SELECT user_id FROM users WHERE role = 'exec' AND can_view_wac = 1 "
                "ORDER BY user_id LIMIT 1")
    uid = cur.fetchone()["user_id"]
pipe = Pipeline(OfflinePlanner())
p = principal_for_user_id(uid)
key = "restore-" + secrets.token_hex(8)
answered = pipe.ask(p, "What is our total revenue this quarter?", idempotency_key=key)
paused = pipe.ask(p, f"What was the volume for {twin} in the last 3 months?")
print(json.dumps({"user_id": uid, "key": key, "answered": answered.status,
                  "headline": answered.answer.headline if answered.answer else None,
                  "paused": paused.status, "conversation_id": paused.conversation_id}))
"""

#: In the restored database: the same key replays the committed answer, and
#: the reply resumes the paused clarification.
CHECK_STATE = r"""
import json, sys
from app.auth.policy import principal_for_user_id
from app.llm.planner import OfflinePlanner
from app.pipeline import Pipeline
s = json.loads(sys.argv[1])
pipe = Pipeline(OfflinePlanner())
p = principal_for_user_id(s["user_id"])
out = {}
try:
    replay = pipe.ask(p, "What is our total revenue this quarter?", idempotency_key=s["key"])
    out.update(replayed=(replay.payload or {}).get("replayed") is True,
               replay_status=replay.status,
               replay_headline=((replay.payload or {}).get("answer") or {}).get("headline"))
except Exception as exc:
    out.update(replayed=False, replay_status=type(exc).__name__, replay_headline=None)
try:
    out["resumed"] = pipe.ask(p, "the second one", conversation_id=s["conversation_id"]).status
except Exception as exc:
    out["resumed"] = type(exc).__name__
print(json.dumps(out))
"""


def answers(db: str, cluster: Cluster = LOCAL) -> dict:
    """The same questions, as a scoped user and an executive, through the
    real pipeline in a child process pointed at `db`."""
    return child(ANSWERS, cluster.app_env(db), json.dumps(QUESTIONS))


def same_cluster(args) -> int:
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


def start_cluster(work: pathlib.Path) -> Cluster:
    port, admin_pw = free_port(), secrets.token_urlsafe(24)
    pwfile = work / "pw"
    pwfile.write_text(admin_pw)
    pwfile.chmod(0o600)
    (work / "sock").mkdir(mode=0o700)
    subprocess.run(["initdb", "-D", str(work / "pg"), "-U", "pacrestore",
                    "--auth=scram-sha-256", f"--pwfile={pwfile}", "--no-instructions"],
                   check=True, capture_output=True)
    pwfile.unlink()
    subprocess.run(["pg_ctl", "-D", str(work / "pg"), "-l", str(work / "postgres.log"), "-w",
                    "-o", f"-p {port} -c listen_addresses=127.0.0.1 "
                          f"-c unix_socket_directories={work / 'sock'}", "start"],
                   check=True, capture_output=True)
    return Cluster(port, "pacrestore", admin_pw,
                   {k: secrets.token_urlsafe(24) for k in PASSWORD_KEYS})


def restore_as_documented(cluster: Cluster, target: str, dump: pathlib.Path,
                          jobs: int) -> dict:
    """docs/RUNBOOK.md §8, new cluster: scripts/bootstrap_db.py (without
    --drop) for the roles, then pg_restore into the database."""
    out: dict = {}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"), "--no-env",
                        "--admin-dsn", cluster.admin_dsn()], cwd=ROOT,
                       env=cluster.app_env(target), capture_output=True, text=True)
    out.update(roles_seconds=round(time.perf_counter() - t0, 2), roles_exit=r.returncode)
    t0 = time.perf_counter()
    r = subprocess.run(["pg_restore", "-d", target, "-j", str(jobs), str(dump)],
                       env=cluster.tool_env(), capture_output=True, text=True)
    errors = [line for line in r.stderr.splitlines() if "error:" in line]
    out.update(restore_seconds=round(time.perf_counter() - t0, 2), restore_exit=r.returncode,
               restore_errors=len(errors), first_errors=[e[:200] for e in errors[:5]])
    return out


def new_cluster(args) -> int:
    copy = f"{args.target}_src"
    work = pathlib.Path(tempfile.mkdtemp(prefix="pac-restore-cluster-"))
    cluster = None
    timings: dict = {}
    try:
        run(["dropdb", "--if-exists", copy])
        t0 = time.perf_counter()
        run(["createdb", "-T", args.source, "-O", "pac_owner", copy])
        timings["copy_source_seconds"] = round(time.perf_counter() - t0, 2)
        state = child(MAKE_STATE, LOCAL.app_env(copy), TWIN)
        # Asking writes (audit, conversations): ask first, then read what the
        # dump will hold.
        reference = answers(copy)
        before, cut = facts(copy), cutoff(copy)
        dump = work / "pac.dump"
        timings["dump_seconds"] = run(["pg_dump", "-Fc", "-f", str(dump), copy])
        dump_mb = round(dump.stat().st_size / 1e6, 1)

        started = time.perf_counter()
        t0 = time.perf_counter()
        cluster = start_cluster(work)
        timings["new_cluster_seconds"] = round(time.perf_counter() - t0, 2)
        procedure = restore_as_documented(cluster, args.target, dump, args.jobs)
        errors: dict[str, str] = {}

        def attempt(name, fn, default):
            try:
                return fn()
            except Exception as exc:          # recorded as a failed check
                errors[name] = f"{type(exc).__name__}: {str(exc).strip().splitlines()[-1][:200]}"
                return default
        # What was restored, read before any check writes to it.
        after = attempt("facts", lambda: facts(args.target, cluster),
                        {k: None for k in before} | {"counts": {}})
        restored_cut = attempt("cutoff", lambda: cutoff(args.target, cluster), None)
        served = attempt("answers", lambda: answers(args.target, cluster),
                         {"boundary_problems": None, "ready_dataset": None, "answers": None})
        resumed = attempt("state", lambda: child(CHECK_STATE, cluster.app_env(args.target),
                                                 json.dumps(state)),
                          {"replayed": False, "replay_status": None, "replay_headline": None,
                           "resumed": None})
        timings["restore_to_ready_seconds"] = round(time.perf_counter() - started, 2)
    finally:
        if cluster is not None:
            subprocess.run(["pg_ctl", "-D", str(work / "pg"), "-m", "fast", "-w", "stop"],
                           capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
        subprocess.run(["dropdb", "--if-exists", copy], capture_output=True)

    checks = {
        "restore_clean": procedure["roles_exit"] == 0 and procedure["restore_exit"] == 0
                         and procedure["restore_errors"] == 0,
        "row_counts_match": before["counts"] == after["counts"],
        "generation_matches": before["generation"] == after["generation"],
        "policies_and_rls_match": (before["policies"], before["rls_tables"])
                                  == (after["policies"], after["rls_tables"]),
        "grants_match": (before["table_grants"], before["column_grants"])
                        == (after["table_grants"], after["column_grants"]),
        "memberships_match": before["memberships"] == after["memberships"],
        "logins_can_connect": len(after["connect"] or []) == 3,
        "boundary_intact": served["boundary_problems"] == [],
        "ready": served["ready_dataset"] == before["generation"],
        "answers_match": served["answers"] == reference["answers"],
        "cutoff_matches": restored_cut == cut,
        "stored_answer_replays": resumed["replayed"] and resumed["replay_status"] == "answered"
                                 and resumed["replay_headline"] == state["headline"],
        "paused_clarification_resumes": state["paused"] == "clarify"
                                        and resumed["resumed"] == "answered",
    }
    print(json.dumps({
        "mode": "new_cluster", "source": args.source, "copy": copy, "target": args.target,
        "procedure": procedure, "dump_mb": dump_mb, "timings": timings, "checks": checks,
        "errors": errors, "state": {"left": {k: state[k] for k in ("answered", "paused")},
                                    "after_restore": resumed},
        "cutoff": {"source_copy": cut, "restored": restored_cut},
        "sales_rows": after["counts"].get("sales"),
        "differences": {k: [before["counts"].get(k), after["counts"].get(k)]
                        for k in before["counts"] if before["counts"][k] != after["counts"].get(k)},
        "memberships": after["memberships"], "answers": served["answers"],
    }, default=str))
    return 0 if all(checks.values()) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.environ.get("PAC_DB_NAME", "pharma_analytics"))
    ap.add_argument("--target", default=None)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--new-cluster", action="store_true",
                    help="restore into a new PostgreSQL cluster created for the drill")
    args = ap.parse_args()
    args.target = args.target or ("pharma_analytics_restored" if args.new_cluster
                                  else "pharma_analytics_restoretest")
    if not args.target.startswith("pharma_analytics_restore"):
        print("the target must be a disposable pharma_analytics_restore* database",
              file=sys.stderr)
        return 2
    if args.target == args.source:
        print("source and target are the same database", file=sys.stderr)
        return 2
    return new_cluster(args) if args.new_cluster else same_cluster(args)


if __name__ == "__main__":
    sys.exit(main())
