#!/usr/bin/env python3
"""Provisioning and restore as a restricted administrator, as on Amazon RDS.

    python3 evidence/probes/restricted_admin_provisioning.py --out result.json

RUNBOOK §1 and §8 run bootstrap_db.py and pg_restore as a PostgreSQL
superuser. RDS gives no superuser: its master user has CREATEROLE and
CREATEDB (through rds_superuser) and nothing that bypasses permissions or
row-level security. This emulates that role in clusters of its own -- a
login role with exactly CREATEROLE and CREATEDB, not superuser, not
BYPASSRLS -- and runs every provisioning step as it, recording each step's
outcome rather than stopping at the first failure:

1. bootstrap_db.py (roles, database, migrations, graph store, logins);
2. load_data.py --mode seed, as the owner the application is given;
3. the runtime security boundary, and the attributes of every pac_ role;
4. an answer through the pipeline, as a seeded executive;
5. pg_dump of that database, as the owner (no superuser exists to take it);
6. into a second new cluster, as the restricted administrator:
   bootstrap_db.py --roles-only; createdb -O pac_owner; pg_restore
   --role=pac_owner (a non-superuser cannot restore with --create: the new
   database belongs to pac_owner and the administrator could not create its
   schemas); bootstrap_db.py --roles-only again, which grants CONNECT on the
   now existing database; then the boundary and an answer again.

An emulation on local PostgreSQL 16: it reproduces RDS's permission model
for these statements, not RDS itself (parameter groups, TLS, IAM
authentication, snapshots). Credentials are generated here and never
printed; the clusters are removed at the end.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADMIN = "rds_master_emul"
DB = "pac_rds"
KEYS = ("OWNER", "AUTH", "EXEC", "SCOPED")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    def __init__(self, work: pathlib.Path, name: str):
        self.dir = work / name
        self.dir.mkdir()
        self.port = free_port()
        self.super_pw, self.admin_pw = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        pw = self.dir / "pw"
        pw.write_text(self.super_pw)
        subprocess.run(["initdb", "-D", str(self.dir / "pg"), "-U", "pgsuper",
                        "--auth=scram-sha-256", f"--pwfile={pw}", "--no-instructions"],
                       check=True, capture_output=True)
        pw.unlink()
        (self.dir / "sock").mkdir(mode=0o700)
        subprocess.run(["pg_ctl", "-D", str(self.dir / "pg"), "-l", str(self.dir / "pg.log"), "-w",
                        "-o", f"-p {self.port} -c listen_addresses=127.0.0.1 "
                              f"-c unix_socket_directories={self.dir / 'sock'}", "start"],
                       check=True, capture_output=True)
        # The RDS master user, as far as these statements can tell: CREATEROLE
        # and CREATEDB, and nothing that bypasses permissions or RLS.
        from psycopg import sql
        self.sql("pgsuper", self.super_pw, "postgres", sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} CREATEROLE CREATEDB "
            "NOSUPERUSER NOBYPASSRLS NOREPLICATION").format(
                sql.Identifier(ADMIN), sql.Literal(self.admin_pw)))

    def sql(self, user, password, db, text, params=()):
        import psycopg
        with psycopg.connect(host="127.0.0.1", port=self.port, dbname=db, user=user,
                             password=password, autocommit=True) as conn:
            cur = conn.execute(text, params or None)
            return cur.fetchall() if cur.description else []

    def admin_dsn(self, db="postgres") -> str:
        from psycopg.conninfo import make_conninfo
        return make_conninfo(host="127.0.0.1", port=self.port, dbname=db, user=ADMIN,
                             password=self.admin_pw)

    def stop(self):
        subprocess.run(["pg_ctl", "-D", str(self.dir / "pg"), "-m", "fast", "-w", "stop"],
                       capture_output=True)


def env(cluster: Cluster, passwords: dict, work: pathlib.Path) -> dict:
    return {"PATH": f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(work), "PYTHONPATH": str(ROOT), "LANG": "C.UTF-8",
            "PAC_DB_HOST": "127.0.0.1", "PAC_DB_PORT": str(cluster.port), "PAC_DB_NAME": DB,
            "PAC_LLM_PROVIDER": "offline", "AWS_EC2_METADATA_DISABLED": "true",
            **{f"PAC_DB_{k}_PASSWORD": v for k, v in passwords.items()}}


def step(name: str, argv: list[str], e: dict, work: pathlib.Path, stdin: str | None = None) -> dict:
    r = subprocess.run(argv, cwd=work, env=e, capture_output=True, text=True, input=stdin,
                       timeout=900)
    text = (r.stderr or "") + (r.stdout or "")
    # The statement PostgreSQL refused, never a value: error lines only.
    errors = [line.strip()[:200] for line in text.splitlines()
              if "ERROR" in line or "Error:" in line or line.startswith(("psycopg", "pg_restore: error"))]
    return {"step": name, "exit": r.returncode, "errors": errors[:4]}


CHECK = r"""
import json
from app.auth.policy import principal_for_user_id
from app.db import owner_transaction, verify_runtime_role_safety
from app.llm.planner import OfflinePlanner
from app.pipeline import Pipeline
out = {"boundary_problems": verify_runtime_role_safety()}
with owner_transaction() as cur:
    cur.execute("SELECT user_id FROM users WHERE role = 'exec' ORDER BY user_id LIMIT 1")
    uid = cur.fetchone()["user_id"]
r = Pipeline(OfflinePlanner()).ask(principal_for_user_id(uid), "What is our total volume this quarter?")
out["answer"] = r.status
print(json.dumps(out))
"""


def check(e: dict, work: pathlib.Path) -> dict:
    r = subprocess.run([sys.executable, "-c", CHECK], cwd=work, env=e, capture_output=True,
                       text=True, timeout=300)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (r.stderr.strip().splitlines() or ["?"])[-1][:200]}


def roles(cluster: Cluster) -> list:
    return [list(r) for r in cluster.sql(
        "pgsuper", cluster.super_pw, "postgres",
        "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin "
        "FROM pg_roles WHERE rolname LIKE 'pac_%' ORDER BY 1")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    work = pathlib.Path(tempfile.mkdtemp(prefix="pac-rds-emul-"))
    passwords = {k: secrets.token_urlsafe(24) for k in KEYS}
    result: dict = {"admin_role": f"{ADMIN}: LOGIN CREATEROLE CREATEDB, not SUPERUSER, "
                                  "not BYPASSRLS, not REPLICATION",
                    "steps": []}
    clusters = []
    try:
        a = Cluster(work, "a")
        clusters.append(a)
        e = env(a, passwords, work)
        boot = step("bootstrap_db.py", [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"),
                                        "--no-env", "--admin-dsn", a.admin_dsn()], e, work)
        result["steps"].append(boot)
        if boot["exit"] == 0:
            result["steps"].append(step("load_data.py --mode seed",
                                        [sys.executable, str(ROOT / "scripts" / "load_data.py"),
                                         "--mode", "seed"], e, work))
            result["after_provisioning"] = check(e, work)
        result["roles"] = roles(a)
        dump = work / "pac.dump"
        dumped = step("pg_dump as the owner",
                      ["pg_dump", "-Fc", "-f", str(dump), "-h", "127.0.0.1", "-p", str(a.port),
                       "-U", "pac_owner", DB], {**e, "PGPASSWORD": passwords["OWNER"]}, work)
        result["steps"].append(dumped)
        if dumped["exit"] == 0:
            b = Cluster(work, "b")
            clusters.append(b)
            eb = env(b, passwords, work)
            result["steps"].append(step("bootstrap_db.py --roles-only (new cluster)",
                                        [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"),
                                         "--roles-only", "--admin-dsn", b.admin_dsn()], eb, work))
            # A non-superuser cannot pg_restore --create: the new database
            # belongs to pac_owner, so the administrator could not create its
            # schemas. Create it, restore as its owner, then grant CONNECT.
            result["steps"].append(step("createdb -O pac_owner as the administrator",
                                        ["createdb", "-h", "127.0.0.1", "-p", str(b.port), "-U", ADMIN,
                                         "-O", "pac_owner", DB],
                                        {**eb, "PGPASSWORD": b.admin_pw}, work))
            result["steps"].append(step("pg_restore --role=pac_owner as the administrator",
                                        ["pg_restore", "-d", DB, "--role=pac_owner", "-j", "4",
                                         "-h", "127.0.0.1", "-p", str(b.port), "-U", ADMIN, str(dump)],
                                        {**eb, "PGPASSWORD": b.admin_pw}, work))
            result["steps"].append(step("bootstrap_db.py --roles-only again (CONNECT)",
                                        [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"),
                                         "--roles-only", "--admin-dsn", b.admin_dsn()], eb, work))
            result["after_restore"] = check(eb, work)
            result["roles_after_restore"] = roles(b)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        for c in clusters:
            c.stop()
        shutil.rmtree(work, ignore_errors=True)
    ok = (all(s["exit"] == 0 for s in result["steps"]) and len(result["steps"]) == 7
          and result.get("after_provisioning", {}).get("boundary_problems") == []
          and result.get("after_provisioning", {}).get("answer") == "answered"
          and result.get("after_restore", {}).get("boundary_problems") == []
          and result.get("after_restore", {}).get("answer") == "answered")
    result["all_steps_passed"] = ok
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
