#!/usr/bin/env python3
"""Certificate-verified TLS to PostgreSQL, for every connection the code makes.

    python3 evidence/probes/db_tls_verify_full.py --out result.json

Settings.dsn() sets no TLS option, so libpq's PGSSLMODE and PGSSLROOTCERT
govern every connection: the serving pools, the graph store, the freshness
reads, the owner's jobs and the scripts. A managed database (RDS) is reached
over the network and must be verified against its CA. This starts a cluster
of its own that accepts TLS connections only (`hostssl` in pg_hba.conf),
with a server certificate for `localhost` signed by a CA made here, and:

1. bootstraps, loads seed data and answers a question through the pipeline
   with PGSSLMODE=verify-full and that CA;
2. checks in pg_stat_ssl that the application's connections were encrypted;
3. checks the same answer is refused with another CA (verification fails)
   and with PGSSLMODE=disable (the server refuses a plaintext connection).

Credentials and keys are generated here and removed with the cluster.
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
DB = "pac_tls"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def openssl(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def make_ca(work: pathlib.Path, name: str) -> pathlib.Path:
    d = work / name
    d.mkdir()
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
            f"/CN=probe {name} CA", "-keyout", "ca.key", "-out", "ca.pem", cwd=d)
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    work = pathlib.Path(tempfile.mkdtemp(prefix="pac-tls-"))
    result: dict = {}
    pg = work / "pg"
    started = False
    try:
        ca, other = make_ca(work, "ca"), make_ca(work, "other")
        (work / "san.cnf").write_text("subjectAltName=DNS:localhost\n")
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                "-keyout", str(work / "server.key"), "-out", str(work / "server.csr"), cwd=work)
        openssl("x509", "-req", "-in", "server.csr", "-CA", str(ca / "ca.pem"), "-CAkey",
                str(ca / "ca.key"), "-CAcreateserial", "-days", "2", "-extfile", "san.cnf",
                "-out", "server.crt", cwd=work)
        (work / "server.key").chmod(0o600)
        port, super_pw = free_port(), secrets.token_urlsafe(24)
        (work / "pw").write_text(super_pw)
        subprocess.run(["initdb", "-D", str(pg), "-U", "pgsuper", "--auth=scram-sha-256",
                        f"--pwfile={work / 'pw'}", "--no-instructions"], check=True,
                       capture_output=True)
        (work / "pw").unlink()
        # TLS connections only, from anywhere this cluster listens.
        (pg / "pg_hba.conf").write_text("hostssl all all 127.0.0.1/32 scram-sha-256\n"
                                        "hostssl all all ::1/128 scram-sha-256\n")
        (work / "sock").mkdir(mode=0o700)
        subprocess.run(["pg_ctl", "-D", str(pg), "-l", str(work / "pg.log"), "-w", "-o",
                        f"-p {port} -c listen_addresses=localhost -c ssl=on "
                        f"-c ssl_cert_file={work / 'server.crt'} -c ssl_key_file={work / 'server.key'} "
                        f"-c unix_socket_directories={work / 'sock'}", "start"],
                       check=True, capture_output=True)
        started = True
        passwords = {k: secrets.token_urlsafe(24) for k in ("OWNER", "AUTH", "EXEC", "SCOPED")}
        base = {"PATH": f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin:/opt/homebrew/bin",
                "HOME": str(work), "PYTHONPATH": str(ROOT), "PAC_DB_HOST": "localhost",
                "PAC_DB_PORT": str(port), "PAC_DB_NAME": DB, "PAC_LLM_PROVIDER": "offline",
                "AWS_EC2_METADATA_DISABLED": "true",
                **{f"PAC_DB_{k}_PASSWORD": v for k, v in passwords.items()}}
        verified = {**base, "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(ca / "ca.pem")}
        admin = (f"host=localhost port={port} dbname=postgres user=pgsuper "
                 f"password={super_pw}")

        def run(name, argv, env):
            r = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=600)
            lines = [line for line in (r.stderr + r.stdout).splitlines()
                     if "rror" in line or "SSL" in line or "certificate" in line]
            return {"step": name, "exit": r.returncode, "errors": [x.strip()[:200] for x in lines[-2:]]}

        result["steps"] = [
            run("bootstrap_db.py (verify-full)", [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"),
                                                  "--no-env", "--admin-dsn", admin], verified),
            run("load_data.py --mode seed (verify-full)",
                [sys.executable, str(ROOT / "scripts" / "load_data.py"), "--mode", "seed"], verified),
        ]
        ask = r"""
import json, psycopg
from app.auth.policy import principal_for_user_id
from app.db import owner_transaction, verify_runtime_role_safety
from app.llm.planner import OfflinePlanner
from app.pipeline import Pipeline
problems = verify_runtime_role_safety()
with owner_transaction() as cur:
    cur.execute("SELECT user_id FROM users WHERE role = 'exec' ORDER BY user_id LIMIT 1")
    uid = cur.fetchone()["user_id"]
    r = Pipeline(OfflinePlanner()).ask(principal_for_user_id(uid), "What is our total volume this quarter?")
    cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE s.ssl) AS tls, "
                "min(s.version) AS version FROM pg_stat_activity a JOIN pg_stat_ssl s USING (pid) "
                "WHERE a.datname = current_database() AND a.usename LIKE 'pac_%'")
    row = cur.fetchone()
print(json.dumps({"answer": r.status, "boundary_problems": problems,
                  "connections": row["n"], "encrypted": row["tls"], "tls_version": row["version"]}))
"""

        def answer(env):
            r = subprocess.run([sys.executable, "-c", ask], cwd=work, env=env, capture_output=True,
                               text=True, timeout=300)
            try:
                return json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                err = [line for line in r.stderr.splitlines() if line.strip()]
                return {"refused": (err[-1] if err else "?")[:200]}

        result["verify_full"] = answer(verified)
        result["another_ca"] = answer({**base, "PGSSLMODE": "verify-full",
                                       "PGSSLROOTCERT": str(other / "ca.pem")})
        result["plaintext"] = answer({**base, "PGSSLMODE": "disable"})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(pg), "-m", "fast", "-w", "stop"], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    v = result.get("verify_full", {})
    ok = (all(s["exit"] == 0 for s in result.get("steps", [])) and v.get("answer") == "answered"
          and v.get("boundary_problems") == [] and v.get("connections", 0) > 0
          and v.get("encrypted") == v.get("connections")
          and "refused" in result.get("another_ca", {}) and "refused" in result.get("plaintext", {}))
    result["passed"] = ok
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
