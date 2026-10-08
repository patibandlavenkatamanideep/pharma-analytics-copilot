#!/usr/bin/env python3
"""Upgrade and code-rollback compatibility, in a PostgreSQL cluster of its own.

    python3 evidence/probes/upgrade_compatibility.py --previous 7950e71 --out result.json

1. The previous release's source (``git archive`` of --previous, unpacked in a
   temporary directory; its source, not its container image) bootstraps a
   new cluster, loads the seed data and serves it. Through its HTTP API it
   leaves state behind: a signed-in session, an answer committed under an
   idempotency key, a clarification paused mid-conversation, and a run whose
   process was killed mid-request (lease running).
2. This checkout's migrations upgrade that database, twice (they must
   converge), timed.
3. This checkout's server on the upgraded database, through HTTP: the old
   session, a password sign-in, the stored answer replayed, the paused
   clarification resumed, the dead run taken over and committed once, a
   RAM's row restriction, a no-WAC executive's pricing restriction, and the
   sign-in methods with single sign-on disabled.
4. The previous server on the same upgraded database, the same checks, then
   each release's full data load: what rolling back the code alone would do.

Step 4 is a measurement, not an approval: the previous release has known
unresolved defects (evidence/ledger.json), one of them an access defect,
so it is not a rollback target whatever this finds. Everything runs on
loopback, credentials are generated here and never printed, and the cluster
is removed at the end. The offline planner; no model is called.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ops_drill", ROOT / "scripts" / "ops_drill.py")
drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drill)
Client, Proc, free_port, wait_until = drill.Client, drill.Proc, drill.free_port, drill.wait_until

TWIN = "Upgrade Probe Twin Facility"
LEASE_S = 5
REVENUE = "What is our total revenue this quarter?"
VOLUME = "total paid pack units last quarter"


class Probe:
    def __init__(self, previous: str, work: pathlib.Path):
        self.previous, self.work = previous, work
        self.logs = work / "logs"
        self.logs.mkdir(parents=True)
        self.prev_src = work / "previous"
        self.port = free_port()
        self.admin_pw = secrets.token_urlsafe(24)
        self.pw = {k: secrets.token_urlsafe(24) for k in ("OWNER", "AUTH", "EXEC", "SCOPED")}
        self.server: Proc | None = None
        self.result: dict = {"previous": previous}

    # -- infrastructure --------------------------------------------------------------

    def unpack_previous(self) -> None:
        self.prev_src.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", self.previous],
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(self.prev_src)], input=archive, check=True)
        self.result["previous_commit"] = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", self.previous], check=True,
            capture_output=True, text=True).stdout.strip()

    def start_cluster(self) -> None:
        pwfile = self.work / "pw"
        pwfile.write_text(self.admin_pw)
        pwfile.chmod(0o600)
        (self.work / "sock").mkdir(mode=0o700)
        subprocess.run(["initdb", "-D", str(self.work / "pg"), "-U", "pacprobe",
                        "--auth=scram-sha-256", f"--pwfile={pwfile}", "--no-instructions"],
                       check=True, capture_output=True)
        pwfile.unlink()
        subprocess.run(["pg_ctl", "-D", str(self.work / "pg"), "-l", str(self.logs / "pg.log"),
                        "-w", "-o", f"-p {self.port} -c listen_addresses=127.0.0.1 "
                                    f"-c unix_socket_directories={self.work / 'sock'}", "start"],
                       check=True, capture_output=True)

    def env(self, src: pathlib.Path, **extra: str) -> dict[str, str]:
        return {"PATH": f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin:/opt/homebrew/bin",
                "HOME": str(self.work), "PYTHONPATH": str(src), "LANG": "C.UTF-8",
                "PAC_DB_HOST": "127.0.0.1", "PAC_DB_PORT": str(self.port),
                "PAC_DB_NAME": "pac_compat", "PAC_LLM_PROVIDER": "offline",
                "AWS_EC2_METADATA_DISABLED": "true", "PAC_COOKIE_SECURE": "false",
                "PAC_RUN_LEASE_SECONDS": str(LEASE_S), "PAC_REQUEST_DEADLINE_SECONDS": "4",
                "PAC_PUBLICATION_SETTLE_SECONDS": "0", "PAC_OIDC_ENABLED": "false",
                **{f"PAC_DB_{k}_PASSWORD": v for k, v in self.pw.items()}, **extra}

    def py(self, src: pathlib.Path, *argv: str, stdin: str | None = None,
           timeout: float = 900) -> subprocess.CompletedProcess:
        # Scripts by path, run from the probe's directory: no repository .env
        # is ever read, so nothing can point a child at another database.
        argv = [str(src / a) if a.startswith("scripts/") else a for a in argv]
        return subprocess.run([sys.executable, *argv], cwd=self.work, env=self.env(src),
                              input=stdin, capture_output=True, text=True, timeout=timeout)

    def admin_sql(self, text: str, params: tuple = ()) -> list[tuple]:
        import psycopg
        with psycopg.connect(host="127.0.0.1", port=self.port, dbname="pac_compat",
                             user="pacprobe", password=self.admin_pw, autocommit=True) as conn:
            cur = conn.execute(text, params or None)      # no params: % is literal
            return cur.fetchall() if cur.description else []

    def serve(self, src: pathlib.Path, name: str) -> None:
        self.http = free_port()
        self.server = Proc(name, [sys.executable, "-m", "uvicorn", "app.api.main:app",
                                  "--host", "127.0.0.1", "--port", str(self.http),
                                  "--log-config", str(src / "app" / "log_config.json")],
                           self.env(src), self.work, self.logs).start()
        wait_until(lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{self.http}/ready", timeout=2).status == 200, f"{name} ready", 120)

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.server is not None:
            self.server.stop(sig)
            self.server = None

    def client(self, user: str) -> Client:
        c = Client(f"http://127.0.0.1:{self.http}")
        status, _, _ = c.call("POST", "/api/login", {"email": self.users[user]["email"],
                                                      "password": self.users[user]["password"]})
        if status != 200:
            raise RuntimeError(f"sign-in {user}: {status}")
        return c

    def ask(self, c: Client, q: str, key: str | None = None, **body) -> tuple[int, dict]:
        status, out, _ = c.call("POST", "/api/ask", {"question": q, **body},
                                headers={"Idempotency-Key": key} if key else None)
        return status, out if isinstance(out, dict) else {}

    # -- steps -----------------------------------------------------------------------

    def previous_release(self) -> None:
        src = self.prev_src
        for step, argv in (("bootstrap", ["scripts/bootstrap_db.py", "--no-env", "--admin-dsn",
                                          f"postgresql://pacprobe:{self.admin_pw}@127.0.0.1:"
                                          f"{self.port}/postgres"]),
                           ("load", ["scripts/load_data.py", "--mode", "seed"])):
            r = self.py(src, *argv)
            if r.returncode != 0:
                raise RuntimeError(f"previous {step}: {r.stderr[-1500:]}")
        own, other = (r[0] for r in self.admin_sql(
            "SELECT * FROM (SELECT DISTINCT territory_name FROM zip_territory z WHERE EXISTS "
            "(SELECT 1 FROM organizations o JOIN sales s USING (org_id) WHERE o.zip = z.zip)) t "
            "ORDER BY 1 LIMIT 2"))
        region = self.admin_sql("SELECT region_name FROM zip_territory WHERE territory_name = %s "
                                "LIMIT 1", (own,))[0][0]
        self.other_territory = other
        tag = secrets.token_hex(3)
        self.users = {}
        rows = []
        for key, role, terr, reg, wac in (("exec", "exec", None, None, 1),
                                          ("nowac", "exec", None, None, 0),
                                          ("ram", "ram", own, region, 0)):
            uid = f"probe-{key}-{tag}"
            self.users[key] = {"user_id": uid, "email": f"{uid}@test.invalid",
                               "password": secrets.token_urlsafe(18)}
            rows.append((uid, self.users[key]["email"], f"Probe {key}", role, terr, reg, wac))
        places = self.admin_sql("SELECT DISTINCT ON (zip) state, zip FROM organizations "
                                "WHERE zip IS NOT NULL ORDER BY zip LIMIT 2")
        for i, (state, zip_code) in enumerate(places):
            self.admin_sql("INSERT INTO organizations (org_id, org_name, org_type, org_status, "
                           "state, zip) VALUES (%s, %s, 'Facility', 'Active', %s, %s)",
                           (f"PROBE-TW{i}-{tag}", TWIN, state, zip_code))
        code = ("import json,sys\nfrom app.db import owner_transaction\n"
                "from app.auth.identity import set_credential\nd=json.loads(sys.stdin.read())\n"
                "with owner_transaction() as cur:\n    for u in d['rows']:\n"
                "        cur.execute('INSERT INTO users (user_id,email,full_name,role,territory_name,"
                "region_name,can_view_wac) VALUES (%s,%s,%s,%s,%s,%s,%s)', u)\n"
                "for uid, pw in d['creds']:\n    set_credential(uid, pw)\n")
        r = self.py(src, "-c", code, stdin=json.dumps(
            {"rows": rows, "creds": [(u["user_id"], u["password"]) for u in self.users.values()]}))
        if r.returncode != 0:
            raise RuntimeError(f"previous users: {r.stderr[-1500:]}")

        self.serve(src, "previous-before")
        self.session = self.client("exec")
        self.key = f"probe-{secrets.token_hex(8)}"
        status, first = self.ask(self.session, REVENUE, self.key)
        self.headline = first.get("answer", {}).get("headline")
        status2, paused = self.ask(self.session, f"What was the volume for {TWIN} in the last 3 months?")
        self.paused_conversation = paused.get("conversation_id")
        # A run left running by a process that died mid-request.
        import psycopg
        self.dead_key = f"probe-dead-{secrets.token_hex(8)}"
        out: dict = {}
        conn = psycopg.connect(host="127.0.0.1", port=self.port, dbname="pac_compat",
                               user="pacprobe", password=self.admin_pw)
        conn.execute("LOCK TABLE sales IN ACCESS EXCLUSIVE MODE")
        worker = threading.Thread(target=lambda: out.update(r=self._quiet_ask(VOLUME)))
        worker.start()
        try:
            wait_until(lambda: self.admin_sql(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = 'pac_compat'")[0][0] >= 1, "request waiting on the lock", 20, 0.05)
            self.stop(signal.SIGKILL)
        finally:
            conn.commit()
            conn.close()
        worker.join(30)
        self.result["state_left_by_previous"] = {
            "answer_under_key": [status, first.get("status"), "$" in (self.headline or "")],
            "paused_clarification": [status2, paused.get("status")],
            "run_left_running": self.admin_sql(
                "SELECT status FROM app_conv.runs WHERE idempotency_key = %s",
                (self.dead_key,))[0][0]}
        self.result["schema_before"] = self.schema()

    def _quiet_ask(self, q: str):
        try:
            return self.ask(self.session, q, self.dead_key)
        except Exception as exc:                     # the process is killed under it
            return type(exc).__name__

    def schema(self) -> dict:
        cols = self.admin_sql("SELECT count(*) FROM information_schema.columns WHERE "
                              "table_schema LIKE 'app_%' OR table_schema = 'public'")[0][0]
        auth = self.admin_sql("SELECT is_nullable, column_default FROM information_schema.columns "
                              "WHERE table_schema = 'app_ref' AND table_name = "
                              "'product_classification' AND column_name = 'authority'")
        return {"columns": cols, "classification_authority": list(auth[0]) if auth else None}

    def upgrade(self) -> None:
        runs = []
        for _ in range(2):
            t0 = time.perf_counter()
            r = self.py(ROOT, "scripts/migrate.py")
            runs.append({"exit": r.returncode, "seconds": round(time.perf_counter() - t0, 2)})
        self.result["upgrade"] = {"runs": runs, "schema_after": self.schema()}
        if any(x["exit"] != 0 for x in runs):
            raise RuntimeError("migration failed")

    def checks(self, src: pathlib.Path, name: str) -> dict:
        self.serve(src, name)
        out: dict = {}
        try:
            status, me, _ = self.session.with_base(f"http://127.0.0.1:{self.http}").call(
                "GET", "/api/me")
            out["existing_session"] = status
            c = self.client("exec")
            out["password_sign_in"] = 200
            status, replay = self.ask(c, REVENUE, self.key)
            out["replay"] = [status, replay.get("replayed"),
                             replay.get("answer", {}).get("headline") == self.headline]
            status, resumed = self.ask(c, "the second one",
                                       conversation_id=self.paused_conversation)
            out["resume_clarification"] = [status, resumed.get("status")]
            # The dead run's lease must have run out before anyone may take it.
            wait_until(lambda: self.admin_sql(
                "SELECT lease_expires_at <= now() FROM app_conv.runs "
                "WHERE idempotency_key = %s", (self.dead_key,))[0][0],
                "the dead run's lease expired", LEASE_S + 30, 0.25)
            status, taken = self.ask(c, VOLUME, self.dead_key)
            turns = self.admin_sql(
                "SELECT count(*) FROM app_conv.turns t JOIN app_conv.runs r USING (run_id) "
                "WHERE r.idempotency_key = %s", (self.dead_key,))[0][0]
            out["dead_run"] = [status, taken.get("status") or (taken.get("detail") or {}).get("code"),
                               taken.get("replayed"), turns]
            ram = self.client("ram")
            status, own = self.ask(ram, VOLUME)
            status_x, exec_v = self.ask(c, VOLUME)
            status_o, other = self.ask(ram, f"Show me sales in the {self.other_territory} territory")
            out["ram_rows"] = [status, own.get("status"),
                               own.get("answer", {}).get("headline")
                               != exec_v.get("answer", {}).get("headline"),
                               other.get("status")]
            nowac = self.client("nowac")
            status, priced = self.ask(nowac, REVENUE)
            out["no_wac_revenue"] = [status, priced.get("status"), "$" in json.dumps(priced)]
            status, methods, _ = c.call("GET", "/api/auth/methods")
            status_s, _, _ = c.call("GET", "/api/auth/oidc/start")
            out["sso_disabled"] = [methods, status_s]
            audit = self.admin_sql("SELECT count(*), count(run_id), count(audit_mode) FROM "
                                   "app_meta.query_audit")[0]
            out["audit_rows_total_with_run_id_with_mode"] = list(audit)
            errors = [line for line in self.server.log.read_text(errors="replace").splitlines()
                      if '"level": "error"' in line]
            out["error_log_lines"] = len(errors)
        finally:
            self.stop()
        return out

    def full_load(self, src: pathlib.Path) -> dict:
        generation = self.admin_sql("SELECT dataset_id FROM app_ref.generation")[0][0]
        t0 = time.perf_counter()
        r = self.py(src, "scripts/load_data.py", "--mode", "seed")
        # The loader's failure line, or the exception's (synthetic seed data).
        failed = [line for line in r.stderr.splitlines() if line.startswith("LOAD ")
                  or (line[:1].isalpha() and ":" in line
                      and line.split(":", 1)[0].rsplit(".", 1)[-1].endswith(
                          ("Error", "Violation", "Exception")))]
        after = self.admin_sql("SELECT dataset_id FROM app_ref.generation")[0][0]
        return {"exit": r.returncode, "seconds": round(time.perf_counter() - t0, 1),
                "message": failed[-1][:240] if failed else None,
                "published_dataset": "unchanged" if after == generation else "replaced"}

    def run(self) -> None:
        self.unpack_previous()
        self.start_cluster()
        self.previous_release()
        self.upgrade()
        self.result["candidate_on_upgraded"] = self.checks(ROOT, "candidate")
        self.result["previous_on_upgraded"] = self.checks(self.prev_src, "previous-after")
        self.result["previous_full_load_on_upgraded"] = self.full_load(self.prev_src)
        self.result["candidate_full_load_on_upgraded"] = self.full_load(ROOT)

    def teardown(self) -> None:
        self.stop(signal.SIGKILL)
        subprocess.run(["pg_ctl", "-D", str(self.work / "pg"), "-m", "fast", "-w", "stop"],
                       capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--previous", required=True, help="the previous release's commit")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    work = pathlib.Path(tempfile.mkdtemp(prefix="pac-upgrade-probe-"))
    probe = Probe(args.previous, work)
    code = 1
    try:
        probe.run()
        cand = probe.result["candidate_on_upgraded"]
        # The candidate must serve the upgraded database with the state the
        # previous release left; the previous release's behaviour is recorded.
        code = 0 if (cand["existing_session"] == 200 and cand["replay"] == [200, True, True]
                     and cand["resume_clarification"][1] in ("answered", "clarify")
                     and cand["dead_run"][:2] == [200, "answered"] and cand["dead_run"][3] == 1
                     and cand["ram_rows"][1] == "answered" and cand["ram_rows"][2]
                     and cand["ram_rows"][3] == "denied"
                     and not cand["no_wac_revenue"][2]
                     and cand["sso_disabled"] == [{"password": True, "oidc": False}, 404]
                     and probe.result["candidate_full_load_on_upgraded"]["exit"] == 0) else 1
    except Exception as exc:
        probe.result["error"] = f"{type(exc).__name__}: {str(exc)[:600]}"
    finally:
        probe.teardown()
        shutil.rmtree(work, ignore_errors=True)
    probe.result["label"] = ("local evidence: the previous release's source run as processes "
                             "(not its image) against one new PostgreSQL cluster, seed data, "
                             "offline planner")
    args.out.write_text(json.dumps(probe.result, indent=1, default=str) + "\n")
    print(json.dumps(probe.result, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
