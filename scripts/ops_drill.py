#!/usr/bin/env python3
"""Local operations drill: two application processes on an isolated PostgreSQL,
a real OpenTelemetry Collector, and Prometheus evaluating the alert rules.

    scripts/fetch_ops_tools.sh <tools>                       # pinned, checksum-verified
    python3 scripts/ops_drill.py --tools <tools> --out <results.json> [--keep]

Everything runs on this machine and is torn down at the end:

* a new PostgreSQL cluster (initdb) in a temporary directory, on its own port,
  password authentication, with credentials generated here and never written
  anywhere but the cluster itself -- the working, release and test databases
  are never touched, and the repository's .env is never read;
* the seed dataset, loaded by the ordinary bootstrap and loader;
* the collector (deploy/observability/otel-collector.yaml, with
  otel-collector.local-files.yaml for the files the scan reads) and Prometheus
  (deploy/observability/prometheus.yml and alerts.yml, with the drill's
  shortened windows and thresholds: DRILL_RULES below);
* two uvicorn processes of the application, each exporting over OTLP;
* the ingestion job (scripts/ingest.py), exporting its own metrics.

Scenarios, each recorded with its evidence: traffic of every outcome on both
processes and the totals Prometheus holds for each; a per-user rate limit
counted across processes; one idempotency key sent to both processes while
the first holds it (a database lock is the barrier, observed in
pg_stat_activity, not a sleep); a refused audit insert; a burst of statement
timeouts; a malformed batch, a batch with old events, a stopped feed and a
resumed one; both application processes stopped and restarted; the collector
killed and restarted while answers continue, and a process stopped while it
is down. Each expected alert must fire, and later clear. At the end every
exported trace and metric, every Prometheus label value and every log is
searched for sentinel values the drill sent in (a question, a key, an email,
territory names, a revenue figure, a session cookie, the database passwords),
and the logs are checked for what diagnosis needs: one JSON object a line,
stable event codes, allowlisted exception types, and ids that lead from a
response to its log lines and from a log line to its trace.

Recovery across processes, each with a database lock or an observed row as
the barrier: a process killed while its request waits, and the same key
taken over by the other process once the lease expires (committed once); two
questions in one conversation on two processes (one waits its turn); a
clarification asked on one process and answered on the other; pricing
revoked between an answer and its replay on the other process; the rate
limit after the user deletes their conversations; and on a third process
with one query slot and one queue place, overload refused at once and a
queued request cancelled from another process.

The lease and request deadline are shortened (LEASE_S, DEADLINE_S) so a
takeover does not wait two minutes; the third process keeps long ones so
its queue holds while the drill acts.

This is local evidence: one machine, one PostgreSQL, processes not
containers, shortened alert windows and lease. It is not a hosted collector,
a hosted backend, an incident drill with on-call, or a load test.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import http.cookiejar
import json
import pathlib
import re
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
OBS = ROOT / "deploy" / "observability"
#: The deployable collector configuration, plus local JSON-lines files that
#: append across restarts (the sentinel scan reads them after one).
COLLECTOR_CONFIGS = [OBS / "otel-collector.yaml", OBS / "otel-collector.local-files.yaml"]
DRILL_VERSION = "1.1.1"
#: Run lease and request deadline for app-1 and app-2 (defaults 120 s and
#: 60 s). The deadline stays shorter than the lease, as config.py says it must.
LEASE_S, DEADLINE_S = 15, 12
#: Two facilities with one name, so a question about it needs a clarification.
TWIN = "Drill Twin Facility"

#: The drill's windows and thresholds, per alert. Each production rule is
#: rewritten by these substitutions only; an alert missing here is a failure,
#: so a new rule cannot be left out silently.
DRILL_RULES: dict[str, dict[str, Any]] = {
    "SlowAnswers": {"sub": {"[10m]": "[1m]"}, "for": "0s"},
    "Errors": {"sub": {"[10m]": "[1m]"}, "for": "0s"},
    "QueryTimeouts": {"sub": {"[10m]": "[1m]"}, "for": "0s"},
    "ModelFailing": {"sub": {"[15m]": "[1m]"}, "for": "0s"},
    "UsageNotReported": {"sub": {"[1h]": "[1m]"}, "for": "0s"},
    "PoolSaturation": {"sub": {"[5m]": "[1m]"}, "for": "0s"},
    "PoolExhausted": {"sub": {"[5m]": "[1m]"}, "for": "0s"},
    "DatabaseErrors": {"sub": {"[5m]": "[1m]"}, "for": "0s"},
    "AuditLoss": {"sub": {"[5m]": "[40s]"}, "for": "0s"},
    "TurnsNotSaved": {"sub": {"[15m]": "[1m]"}, "for": "0s"},
    "MissedIngestionRun": {"sub": {"> 93600": "> 25"}, "for": "0s"},
    "DataNotMoving": {"sub": {"> 259200": "> 3600"}, "for": "0s"},
    "FreshnessNotReported": {"sub": {"[30m]": "[20s]"}, "for": "0s"},
    "BatchRejected": {"sub": {}, "for": "0s"},
    "QuarantineRising": {"sub": {}, "for": "0s"},
    "TelemetryPipelineDown": {"sub": {}, "for": "0s"},
}
NOT_EXERCISED = {
    "SlowAnswers": "no answer was made slow on purpose",
    "ModelFailing": "the offline planner calls no model",
    "UsageNotReported": "the offline planner calls no model",
    "PoolSaturation": "pools were not saturated",
    "PoolExhausted": "pools were not exhausted",
    "DatabaseErrors": "the database stayed reachable",
    "TurnsNotSaved": "turn persistence was not broken",
    "QuarantineRising": "fewer than 100 events were quarantined",
}


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check: Callable[[], Any], what: str, timeout: float = 60.0,
               every: float = 0.25) -> Any:
    """Poll an observable condition; never a fixed sleep standing in for one."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except Exception as exc:                       # the condition is not yet observable
            last_error = exc
        time.sleep(every)
    raise TimeoutError(f"{what}: not observed within {timeout:.0f} s"
                       + (f" (last error: {type(last_error).__name__})" if last_error else ""))


class Proc:
    def __init__(self, name: str, argv: list[str], env: dict[str, str], cwd: pathlib.Path,
                 log_dir: pathlib.Path):
        self.name, self.argv, self.env, self.cwd = name, argv, env, cwd
        self.log = log_dir / f"{name}.log"
        self.p: subprocess.Popen | None = None

    def start(self) -> "Proc":
        fh = self.log.open("ab")
        self.p = subprocess.Popen(self.argv, env=self.env, cwd=self.cwd, stdout=fh,
                                  stderr=subprocess.STDOUT, start_new_session=True)
        return self

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 30.0) -> int | None:
        if not self.alive():
            return None if self.p is None else self.p.returncode
        self.p.send_signal(sig)
        try:
            return self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            return self.p.wait(10)

    def rss_kib(self) -> int | None:
        if not self.alive():
            return None
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.p.pid)],
                             capture_output=True, text=True).stdout.strip()
        return int(out) if out else None


class Client:
    """One signed-in browser: a cookie jar against one process."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None, timeout: float = 90) -> tuple[int, Any, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw or b"null"), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"null"), dict(e.headers)
            except ValueError:
                return e.code, raw.decode(errors="replace"), dict(e.headers)

    def with_base(self, base: str) -> "Client":
        """The same session against the other process."""
        other = Client(base)
        other.jar = self.jar
        other.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        return other

    def cookie_values(self) -> list[str]:
        return [c.value for c in self.jar]


class Prom:
    def __init__(self, base: str):
        self.base = base

    def get(self, path: str, **params) -> Any:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        with urllib.request.urlopen(url, timeout=10) as r:
            body = json.loads(r.read())
        if body.get("status") != "success":
            raise RuntimeError(f"prometheus {path}: {body}")
        return body["data"]

    def query(self, expr: str) -> list[dict]:
        return self.get("/api/v1/query", query=expr)["result"]

    def scalar(self, expr: str) -> float | None:
        result = self.query(expr)
        return float(result[0]["value"][1]) if result else None

    def alerts(self) -> dict[str, str]:
        """alertname -> 'firing' | 'pending' (absent when inactive)."""
        states: dict[str, str] = {}
        for a in self.get("/api/v1/alerts")["alerts"]:
            name = a["labels"]["alertname"]
            if states.get(name) != "firing":
                states[name] = a["state"]
        return states


# ---------------------------------------------------------------------------
# the drill
# ---------------------------------------------------------------------------

class Drill:
    def __init__(self, tools: pathlib.Path, work: pathlib.Path):
        self.tools, self.work = tools, work
        self.logs = work / "logs"
        self.logs.mkdir(parents=True)
        self.results: list[dict] = []
        self.procs: list[Proc] = []
        self.sentinels: dict[str, str] = {}
        self.asks: dict[str, int] = {"app-1": 0, "app-2": 0}
        self.response_ids: list[str] = []
        self.started = time.monotonic()
        self.lock = threading.Lock()

    # -- results ----------------------------------------------------------------

    def record(self, name: str, ok: bool, **evidence: Any) -> bool:
        self.results.append({"scenario": name, "status": "passed" if ok else "failed",
                             "at_s": round(time.monotonic() - self.started, 1), **evidence})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)
        return ok

    def attempt(self, name: str, fn: Callable[[], dict]) -> None:
        try:
            evidence = fn() or {}
            ok = evidence.pop("ok", True)
            self.record(name, ok, **evidence)
        except Exception as exc:
            self.record(name, False, error=f"{type(exc).__name__}: {str(exc)[:300]}")

    # -- infrastructure -------------------------------------------------------------

    def start_postgres(self) -> None:
        self.pg_port = free_port()
        self.admin_pw = secrets.token_urlsafe(24)
        pwfile = self.work / "pg.pw"
        pwfile.write_text(self.admin_pw)
        pwfile.chmod(0o600)
        data, sock = self.work / "pg", self.work / "sock"
        sock.mkdir(mode=0o700)
        subprocess.run(["initdb", "-D", str(data), "-U", "pacdrill", "--auth=scram-sha-256",
                        f"--pwfile={pwfile}", "--no-instructions"], check=True,
                       capture_output=True)
        pwfile.unlink()
        subprocess.run(["pg_ctl", "-D", str(data), "-l", str(self.logs / "postgres.log"), "-w",
                        "-o", f"-p {self.pg_port} -c listen_addresses=127.0.0.1 "
                              f"-c unix_socket_directories={sock} -c max_connections=200",
                        "start"], check=True, capture_output=True)
        self.pg_data = data

    def base_env(self) -> dict[str, str]:
        """Only what the drill sets: nothing inherited that could point a
        process at another database, a provider or a cloud account."""
        if not hasattr(self, "db_pw"):
            self.db_pw = {k: secrets.token_urlsafe(24) for k in ("OWNER", "AUTH", "EXEC", "SCOPED")}
        env = {"PATH": f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin:/opt/homebrew/bin",
               "HOME": str(self.work), "PYTHONPATH": str(ROOT), "LANG": "C.UTF-8",
               "PAC_DB_HOST": "127.0.0.1", "PAC_DB_PORT": str(self.pg_port),
               "PAC_DB_NAME": "pac_drill", "PAC_LLM_PROVIDER": "offline",
               "AWS_EC2_METADATA_DISABLED": "true", "PAC_PUBLICATION_SETTLE_SECONDS": "0",
               "PAC_RELEASE": "ops-drill"}
        env.update({f"PAC_DB_{k}_PASSWORD": v for k, v in self.db_pw.items()})
        return env

    def run_py(self, *argv: str, env: dict[str, str] | None = None, check: bool = True,
               timeout: float = 600) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, *argv], env=env or self.base_env(), cwd=self.work,
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{argv[0]} exited {r.returncode}: {r.stderr[-1500:]}")
        return r

    def bootstrap(self) -> None:
        self.run_py(str(ROOT / "scripts" / "bootstrap_db.py"), "--no-env", "--admin-dsn",
                    f"postgresql://pacdrill:{self.admin_pw}@127.0.0.1:{self.pg_port}/postgres")
        self.run_py(str(ROOT / "scripts" / "load_data.py"), "--mode", "seed")

    def admin_sql(self, text: str, params: tuple = (), db: str = "pac_drill") -> list[tuple]:
        import psycopg
        with psycopg.connect(host="127.0.0.1", port=self.pg_port, dbname=db, user="pacdrill",
                             password=self.admin_pw, autocommit=True) as conn:
            cur = conn.execute(text, params)
            return cur.fetchall() if cur.description else []

    def make_users(self) -> None:
        """Drill identities, their passwords only in this process."""
        tag = secrets.token_hex(4)
        territories = [r[0] for r in self.admin_sql(
            "SELECT DISTINCT territory_name FROM zip_territory z WHERE EXISTS (SELECT 1 FROM "
            "organizations o JOIN sales s ON s.org_id = o.org_id WHERE o.zip = z.zip) ORDER BY 1")]
        own, other = territories[0], territories[-1]
        region = self.admin_sql("SELECT region_name FROM zip_territory WHERE territory_name = %s "
                                "LIMIT 1", (own,))[0][0]
        self.sentinels.update({"email_tag": f"sentinelmail{tag}", "own_territory": own,
                               "other_territory": other})
        self.users: dict[str, dict] = {}
        rows = []
        # A pool of workers keeps every user inside the default limits (20 a
        # minute, 2 at once) except where a limit is the point of the scenario.
        for key, role, wac, terr, reg in (("exec", "exec", 1, None, None),
                                          ("ram", "ram", 0, own, region),
                                          ("rate", "exec", 0, None, None),
                                          ("dup", "exec", 1, None, None),
                                          *((f"w{i}", "exec", 0, None, None) for i in range(6)),
                                          # one per recovery scenario, so no limit is shared
                                          ("kill", "exec", 0, None, None),
                                          ("conv", "exec", 0, None, None),
                                          ("clar", "exec", 1, None, None),
                                          ("rev", "exec", 1, None, None),
                                          ("quota", "exec", 0, None, None),
                                          *((f"o{i}", "exec", 0, None, None) for i in range(3))):
            uid = f"drill-{key}-{tag}"
            email = f"{uid}-sentinelmail{tag}@test.invalid"
            rows.append((uid, email, f"Drill {key}", role, terr, reg, wac))
            self.users[key] = {"user_id": uid, "email": email, "password": secrets.token_urlsafe(18)}
        env = self.base_env()
        code = ("import json,sys\nfrom app.db import owner_transaction\n"
                "from app.auth.identity import set_credential\n"
                "users=json.loads(sys.stdin.read())\n"
                "with owner_transaction() as cur:\n"
                "    for u in users['rows']:\n"
                "        cur.execute('INSERT INTO users (user_id,email,full_name,role,territory_name,"
                "region_name,can_view_wac) VALUES (%s,%s,%s,%s,%s,%s,%s)', u)\n"
                "for uid, pw in users['creds']:\n"
                "    set_credential(uid, pw)\n")
        r = subprocess.run([sys.executable, "-c", code], env=env, cwd=self.work, text=True,
                           input=json.dumps({"rows": rows, "creds": [
                               (u["user_id"], u["password"]) for u in self.users.values()]}),
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"users: {r.stderr[-1500:]}")

    def add_twins(self) -> None:
        """Two facilities sharing TWIN as their name, before any process
        builds its entity index."""
        places = self.admin_sql("SELECT DISTINCT ON (zip) state, zip FROM organizations "
                                "WHERE zip IS NOT NULL ORDER BY zip LIMIT 2")
        tag = secrets.token_hex(3).upper()
        self.twins = [f"DRILL-TW{i}-{tag}" for i in (1, 2)]
        for org_id, (state, zip_code) in zip(self.twins, places):
            self.admin_sql("INSERT INTO organizations (org_id, org_name, org_type, org_status, "
                           "state, zip) VALUES (%s, %s, 'Facility', 'Active', %s, %s)",
                           (org_id, TWIN, state, zip_code))

    def start_collector(self) -> None:
        self.otlp_port, self.prom_exp_port = free_port(), free_port()
        (self.work / "otel").mkdir(exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.work),
               "PAC_OTEL_RECEIVER": f"127.0.0.1:{self.otlp_port}",
               "PAC_OTEL_PROMETHEUS": f"127.0.0.1:{self.prom_exp_port}",
               "PAC_OTEL_FILE_DIR": str(self.work / "otel"),
               "PAC_OTEL_METRIC_EXPIRATION": "20s"}
        argv = [str(self.tools / "otelcol" / "otelcol-contrib")]
        for config in COLLECTOR_CONFIGS:
            argv += ["--config", str(config)]
        self.collector = Proc("otel-collector", argv, env, self.work, self.logs).start()
        self.procs.append(self.collector)
        wait_until(lambda: self._port_open(self.otlp_port) and self._port_open(self.prom_exp_port),
                   "collector listening", 60)

    def restart_collector(self) -> None:
        self.collector = Proc("otel-collector", self.collector.argv, self.collector.env, self.work,
                              self.logs).start()
        self.procs.append(self.collector)
        wait_until(lambda: self._port_open(self.otlp_port), "collector listening again", 60)

    @staticmethod
    def _port_open(port: int) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def drill_rules(self) -> str:
        import yaml
        doc = yaml.safe_load((OBS / "alerts.yml").read_text())
        names = []
        for group in doc["groups"]:
            for rule in group["rules"]:
                name = rule["alert"]
                names.append(name)
                if name not in DRILL_RULES:
                    raise SystemExit(f"alert {name} has no drill rule (DRILL_RULES)")
                for old, new in DRILL_RULES[name]["sub"].items():
                    if old not in rule["expr"]:
                        raise SystemExit(f"alert {name}: {old!r} not in its expression")
                    rule["expr"] = rule["expr"].replace(old, new)
                rule["for"] = DRILL_RULES[name]["for"]
        if set(DRILL_RULES) - set(names):
            raise SystemExit(f"drill rules for missing alerts: {set(DRILL_RULES) - set(names)}")
        self.alert_names = names
        return yaml.safe_dump(doc, sort_keys=False)

    def start_prometheus(self) -> None:
        import yaml
        self.prom_port = free_port()
        (self.work / "drill_alerts.yml").write_text(self.drill_rules())
        config = yaml.safe_load((OBS / "prometheus.yml").read_text())
        config["global"] = {"scrape_interval": "2s", "evaluation_interval": "2s"}
        config["rule_files"] = [str(self.work / "drill_alerts.yml")]
        config["scrape_configs"][0]["static_configs"] = [
            {"targets": [f"127.0.0.1:{self.prom_exp_port}"]}]
        (self.work / "prometheus.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        promtool = str(self.tools / "prometheus" / "promtool")
        checks = {}
        for label, path in (("production rules", OBS / "alerts.yml"),
                            ("drill rules", self.work / "drill_alerts.yml")):
            r = subprocess.run([promtool, "check", "rules", str(path)], capture_output=True, text=True)
            checks[label] = r.returncode == 0
        r = subprocess.run([promtool, "check", "config", "--syntax-only",
                            str(self.work / "prometheus.yml")], capture_output=True, text=True)
        checks["drill config"] = r.returncode == 0
        self.record("promtool accepts the rules and configuration", all(checks.values()),
                    checks=checks)
        self.prometheus = Proc("prometheus", [
            str(self.tools / "prometheus" / "prometheus"),
            f"--config.file={self.work / 'prometheus.yml'}",
            f"--storage.tsdb.path={self.work / 'prom'}",
            f"--web.listen-address=127.0.0.1:{self.prom_port}"],
            {"PATH": "/usr/bin:/bin", "HOME": str(self.work)}, self.work, self.logs).start()
        self.procs.append(self.prometheus)
        self.prom = Prom(f"http://127.0.0.1:{self.prom_port}")
        wait_until(lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{self.prom_port}/-/ready", timeout=2).status == 200,
            "prometheus ready", 60)

    def app_env(self) -> dict[str, str]:
        # Plain HTTP on loopback, as the browser journeys and load test run:
        # a Secure cookie would never be sent back.
        # The source inventory is the operator's list of feeds that may label
        # a metric (app/telemetry.py); a feed outside it is reported as "other"
        # by counters and not at all by the freshness gauges.
        return {**self.base_env(), "PAC_OTEL_ENDPOINT": f"http://127.0.0.1:{self.otlp_port}",
                "PAC_COOKIE_SECURE": "false", "PAC_OTEL_SOURCE_NAMES": "drill-feed",
                "PAC_RUN_LEASE_SECONDS": str(LEASE_S),
                "PAC_REQUEST_DEADLINE_SECONDS": str(DEADLINE_S)}

    def start_app(self, name: str, extra: dict[str, str] | None = None,
                  wait: bool = True) -> Proc:
        """A new process is a new instance: the questions counted for it
        start again."""
        if not hasattr(self, "apps"):
            self.apps, self.app_ports = {}, {}
        port = self.app_ports.setdefault(name, free_port())
        proc = Proc(name, [sys.executable, "-m", "uvicorn", "app.api.main:app",
                           "--host", "127.0.0.1", "--port", str(port),
                           "--timeout-graceful-shutdown", "20",
                           "--log-config", str(ROOT / "app" / "log_config.json")],
                    {**self.app_env(), **(extra or {})}, self.work, self.logs).start()
        self.apps[name] = proc
        self.procs.append(proc)
        with self.lock:
            self.asks[name] = 0
        if wait:
            self.wait_ready(name)
        return proc

    def wait_ready(self, name: str) -> None:
        port = self.app_ports[name]
        wait_until(lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{port}/ready", timeout=2).status == 200, f"{name} ready", 120)

    def start_apps(self) -> None:
        for name in ("app-1", "app-2"):
            self.start_app(name, wait=False)
        for name in ("app-1", "app-2"):
            self.wait_ready(name)

    def stop_app(self, name: str, sig: int = signal.SIGTERM) -> int | None:
        """Stopped for good: its series expire, so its questions no longer count."""
        code = self.apps.pop(name).stop(sig)
        with self.lock:
            self.asks.pop(name, None)
        return code

    def base(self, app: str) -> str:
        return f"http://127.0.0.1:{self.app_ports[app]}"

    def sign_in(self, user: str, app: str) -> Client:
        c = Client(self.base(app))
        status, body, _ = c.call("POST", "/api/login", {"email": self.users[user]["email"],
                                                         "password": self.users[user]["password"]})
        if status != 200:
            raise RuntimeError(f"sign-in {user}@{app}: {status}")
        self.sentinels.setdefault("cookies", "")
        self.sentinels["cookies"] += "|".join(c.cookie_values()) + "|"
        return c

    def ask(self, client: Client, app: str, question: str, key: str | None = None,
            timeout: float = 90, conversation_id: str | None = None,
            include_sql: bool = False) -> tuple[int, Any, dict]:
        with self.lock:
            self.asks[app] += 1
        body: dict[str, Any] = {"question": question}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if include_sql:
            body["include_sql"] = True        # the typed plan comes with it
        out = client.call("POST", "/api/ask", body,
                          headers={"Idempotency-Key": key} if key else None, timeout=timeout)
        rid = {k.lower(): v for k, v in out[2].items()}.get("x-request-id")
        if rid:
            with self.lock:
                self.response_ids.append(rid)
        return out

    def prom_total(self, expr: str) -> float:
        return self.prom.scalar(expr) or 0.0

    def wait_alert(self, name: str, state: str = "firing", timeout: float = 120) -> float:
        t0 = time.monotonic()
        if state == "inactive":
            wait_until(lambda: name not in self.prom.alerts(), f"{name} clears", timeout, 1.0)
        else:
            wait_until(lambda: self.prom.alerts().get(name) == state, f"{name} {state}", timeout, 1.0)
        return round(time.monotonic() - t0, 1)

    # -- scenarios -------------------------------------------------------------------

    def s_traffic(self) -> dict:
        """Every outcome on both processes; each process exports its own series,
        and Prometheus holds exactly the questions each process received."""
        exec1, ram2 = self.sign_in("exec", "app-1"), self.sign_in("ram", "app-2")
        exec2 = exec1.with_base(self.base("app-2"))
        q_sentinel = f"ZZQSENTINEL{secrets.token_hex(3).upper()}"
        self.sentinels["question"] = q_sentinel
        statuses = []
        for client, app, q in (
                (exec1, "app-1", "What is our total revenue this quarter?"),
                (exec1, "app-1", f"What is the volume for {q_sentinel} this quarter?"),
                (exec2, "app-2", "What is our total volume this quarter?"),
                (ram2, "app-2", "What is our total volume this quarter?"),
                (ram2, "app-2", f"Show me sales in the {self.sentinels['other_territory']} territory")):
            status, body, _ = self.ask(client, app, q)
            statuses.append((app, status, (body or {}).get("status")))
            if "revenue" in q and status == 200:
                headline = body["answer"]["headline"]
                money = re.findall(r"\$[\d,]+(?:\.\d+)?", headline)
                if money:
                    self.sentinels["revenue_figure"] = money[0]
        outcome_set = {s for _, _, s in statuses}
        expected = {"answered", "clarify", "denied"}
        # Two processes, two series: the exporter keys a series by the
        # process's identity (service.instance.id -> instance).
        try:
            instances = wait_until(lambda: (self.prom.scalar(
                "count(count by (instance) (pac_ask_outcomes_total))") or 0) >= 2,
                "both processes reporting as distinct instances", 45)
        except TimeoutError:
            instances = False
        totals_ok = self.totals_match(timeout=45)
        return {"ok": expected <= outcome_set and bool(instances) and totals_ok,
                "outcomes": statuses, "distinct_instances": bool(instances),
                "totals_match_requests_received": totals_ok, "asks": dict(self.asks)}

    def totals_match(self, timeout: float = 45) -> bool:
        """Prometheus' sum of outcomes equals the questions the processes received."""
        want = sum(self.asks.values())
        try:
            wait_until(lambda: self.prom_total("sum(pac_ask_outcomes_total)") == want,
                       f"outcomes total {want}", timeout, 1.0)
            return True
        except TimeoutError:
            self.last_total = self.prom_total("sum(pac_ask_outcomes_total)")
            return False

    def s_rate_limit(self) -> dict:
        """The per-user limit is counted in the database, so it holds across processes."""
        per_minute = 20
        clients = {"app-1": self.sign_in("rate", "app-1")}
        clients["app-2"] = clients["app-1"].with_base(self.base("app-2"))
        codes = []
        for i in range(per_minute + 1):
            app = "app-1" if i % 2 == 0 else "app-2"
            status, body, headers = self.ask(clients[app], app, "What is our total volume this quarter?")
            codes.append(status)
        refused = codes[-1] == 429
        return {"ok": codes[:per_minute] == [200] * per_minute and refused,
                "first_20": sorted(set(codes[:per_minute])), "twenty_first": codes[-1]}

    def s_duplicate_key(self) -> dict:
        """One key on both processes while the first holds it. The barrier is
        a lock on sales, seen as a lock wait in pg_stat_activity."""
        import psycopg
        dup1 = self.sign_in("dup", "app-1")
        dup2 = dup1.with_base(self.base("app-2"))
        key = f"drill-dup-{secrets.token_hex(8)}"
        self.sentinels["idempotency_key"] = key
        out: dict = {}
        with psycopg.connect(host="127.0.0.1", port=self.pg_port, dbname="pac_drill",
                             user="pacdrill", password=self.admin_pw) as conn:
            conn.execute("LOCK TABLE sales IN ACCESS EXCLUSIVE MODE")
            worker = threading.Thread(target=lambda: out.setdefault("first", self.ask(
                dup1, "app-1", "What is our total volume this quarter?", key)))
            worker.start()
            wait_until(lambda: self.admin_sql(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = 'pac_drill'")[0][0] >= 1, "first request waiting on the lock", 10, 0.05)
            second = self.ask(dup2, "app-2", "What is our total volume this quarter?", key)
            conn.commit()                                     # releases the lock
        worker.join(60)
        first = out["first"]
        replay = self.ask(dup2, "app-2", "What is our total volume this quarter?", key)
        same = (replay[0] == 200 and replay[1].get("replayed") is True
                and replay[1]["request_id"] == first[1]["request_id"]
                and replay[1]["answer"]["headline"] == first[1]["answer"]["headline"])
        return {"ok": first[0] == 200 and second[0] == 409
                and (second[1] or {}).get("detail", {}).get("code") == "same_request_running"
                and same,
                "first": first[0], "second_on_other_process": second[0],
                "second_code": (second[1] or {}).get("detail", {}).get("code"),
                "replay_on_other_process_identical": same}

    def s_audit_loss(self) -> dict:
        exec1 = self.sign_in("exec", "app-1")
        self.admin_sql("REVOKE INSERT ON app_meta.query_audit FROM pac_auth")
        try:
            status, body, _ = self.ask(exec1, "app-1", "What is our total volume this quarter?")
        finally:
            self.admin_sql("GRANT INSERT ON app_meta.query_audit TO pac_auth")
        fired = self.wait_alert("AuditLoss", "firing", 60)
        return {"ok": status == 200, "answer_status": status,
                "alert": "AuditLoss", "fired_after_s": fired}

    def s_timeouts(self) -> dict:
        """A burst of statement timeouts on both processes, under a held lock."""
        import psycopg
        workers = [(self.sign_in(f"w{i}", "app-1" if i % 2 == 0 else "app-2"),
                    "app-1" if i % 2 == 0 else "app-2") for i in range(6)]
        before = self.prom_total('sum(pac_db_errors_total{kind="timeout"})')
        results = []
        # Two waves, each held past the statement timeout: a sustained
        # condition, as the ratio alerts are written for.
        for wave in range(2):
            with psycopg.connect(host="127.0.0.1", port=self.pg_port, dbname="pac_drill",
                                 user="pacdrill", password=self.admin_pw) as conn:
                conn.execute("LOCK TABLE sales IN ACCESS EXCLUSIVE MODE")
                with futures.ThreadPoolExecutor(6) as pool:
                    calls = [pool.submit(self.ask, c, app, "What is our total volume this quarter?")
                             for c, app in workers]
                    results += [f.result() for f in calls]
                conn.commit()
            wait_until(lambda wave=wave: self.prom_total(
                'sum(pac_db_errors_total{kind="timeout"})') >= before + 6 * (wave + 1),
                f"wave {wave + 1}: its timeouts exported", 45, 1.0)
        statuses = sorted({(r[0], (r[1] or {}).get("status")) for r in results})
        fired = {name: self.wait_alert(name, "firing", 60) for name in ("QueryTimeouts", "Errors")}
        return {"ok": all(s[0] == 200 and s[1] == "error" for s in statuses),
                "responses": statuses, "fired_after_s": fired}

    def batch(self, batch_id: str, events: list[dict]) -> pathlib.Path:
        doc = {"source_system": "drill-feed", "batch_id": batch_id,
               "declared_count": len(events),
               "declared_pack_units": sum(e["pack_units"] for e in events), "events": events}
        path = self.work / f"{batch_id}.json"
        path.write_text(json.dumps(doc))
        return path

    def ingest(self, *paths: pathlib.Path) -> subprocess.CompletedProcess:
        return self.run_py(str(ROOT / "scripts" / "ingest.py"), *map(str, paths),
                           env=self.app_env(), check=False, timeout=300)

    def sale(self, sid: str, when: datetime) -> dict:
        org, ndc, wac = self.admin_sql(
            "SELECT s.org_id, s.ndc, s.wac / s.pack_units FROM sales s WHERE s.data_source = "
            "'distributor' AND s.pack_units > 0 AND s.wac > 0 ORDER BY s.sale_id LIMIT 1")[0]
        return {"source_event_id": sid, "event_version": 1, "kind": "upsert",
                "event_time": when.isoformat(), "org_id": org, "ndc": ndc,
                "data_source": "distributor", "pack_units": 4.0, "unit": "packs",
                "wac": round(float(wac), 2)}

    def s_feed(self) -> dict:
        """A malformed batch; then a valid batch of OLD events; then nothing
        (a stopped job); then a batch of current events."""
        evidence: dict = {}
        # Control totals that do not reconcile: the contract rejects the batch whole.
        bad = self.batch("drill-bad", [self.sale("drill-bad-1", datetime.now(timezone.utc)
                                                 - timedelta(days=2))])
        doc = json.loads(bad.read_text())
        doc["declared_count"] = 3
        bad.write_text(json.dumps(doc))
        r = self.ingest(bad)
        evidence["malformed_exit"] = r.returncode
        evidence["BatchRejected_fired_after_s"] = self.wait_alert("BatchRejected", "firing", 60)

        latest = self.admin_sql("SELECT max(week_ending_date) FROM app_ref.calendar")[0][0]
        old = datetime.fromisoformat(str(latest)).replace(hour=12, tzinfo=timezone.utc) \
            - timedelta(days=1)
        r = self.ingest(self.batch("drill-old", [self.sale("drill-old-1", old)]))
        evidence["old_batch_exit"] = r.returncode
        # Accepted: the feed recovered, so the rejection no longer pages.
        evidence["BatchRejected_cleared_after_s"] = self.wait_alert("BatchRejected", "inactive", 60)
        evidence["DataNotMoving_fired_after_s"] = self.wait_alert("DataNotMoving", "firing", 60)
        evidence["missed_run_not_yet"] = "MissedIngestionRun" not in self.prom.alerts()
        # No further batch: the job is stopped. Freshness keeps ageing.
        evidence["MissedIngestionRun_fired_after_s"] = self.wait_alert(
            "MissedIngestionRun", "firing", 90)

        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        r = self.ingest(self.batch("drill-now", [self.sale("drill-now-1", now)]))
        evidence["current_batch_exit"] = r.returncode
        evidence["MissedIngestionRun_cleared_after_s"] = self.wait_alert(
            "MissedIngestionRun", "inactive", 60)
        evidence["DataNotMoving_cleared_after_s"] = self.wait_alert("DataNotMoving", "inactive", 60)
        evidence["ok"] = (evidence["malformed_exit"] != 0 and evidence["old_batch_exit"] == 0
                          and evidence["current_batch_exit"] == 0 and evidence["missed_run_not_yet"])
        return evidence

    def s_refresh_seen_by_both(self) -> dict:
        """The batch above moved the reporting anchor; both processes answer
        from the new snapshot."""
        exec1 = self.sign_in("exec", "app-1")
        exec2 = exec1.with_base(self.base("app-2"))
        through = []
        for c, app in ((exec1, "app-1"), (exec2, "app-2")):
            status, body, _ = self.ask(c, app, "What is our total volume this quarter?")
            through.append((body or {}).get("answer", {}).get("data_through"))
        latest = str(self.admin_sql("SELECT max(transaction_date) FROM sales")[0][0])
        return {"ok": through[0] == through[1] == latest, "data_through": through,
                "database": latest}

    # -- recovery across processes ---------------------------------------------------

    def held_lock(self):
        """An open transaction holding sales: every analytical query waits on
        it, visibly, until it commits."""
        import psycopg
        conn = psycopg.connect(host="127.0.0.1", port=self.pg_port, dbname="pac_drill",
                               user="pacdrill", password=self.admin_pw)
        conn.execute("LOCK TABLE sales IN ACCESS EXCLUSIVE MODE")
        return conn

    def lock_waiters(self) -> int:
        return self.admin_sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = "
                              "'Lock' AND datname = 'pac_drill'")[0][0]

    def run_row(self, user: str, key: str) -> dict | None:
        rows = self.admin_sql(
            "SELECT run_id, status, conversation_id, lease_expires_at <= now() "
            "FROM app_conv.runs WHERE owner_user_id = %s AND idempotency_key = %s",
            (self.users[user]["user_id"], key))
        if not rows:
            return None
        run_id, status, conversation_id, expired = rows[0]
        return {"run_id": run_id, "status": status, "conversation_id": conversation_id,
                "lease_expired": expired}

    def turns_of(self, conversation_id: str) -> list[tuple]:
        return self.admin_sql("SELECT seq, run_id FROM app_conv.turns "
                              "WHERE conversation_id = %s ORDER BY seq", (conversation_id,))

    def background(self, fn: Callable[[], Any]) -> tuple[threading.Thread, dict]:
        out: dict = {}

        def target():
            t0 = time.monotonic()
            try:
                out["result"] = fn()
            except Exception as exc:                  # a killed process drops the connection
                out["error"] = type(exc).__name__
            out["took_s"] = round(time.monotonic() - t0, 2)
        worker = threading.Thread(target=target)
        worker.start()
        return worker, out

    def s_worker_killed(self) -> dict:
        """A process killed while its request waits. The same key on the other
        process is refused while the lease lives, then taken over and
        committed once; the restarted process replays that commit."""
        q = "What is our total volume this quarter?"
        c1 = self.sign_in("kill", "app-1")
        c2 = c1.with_base(self.base("app-2"))
        key = f"drill-kill-{secrets.token_hex(8)}"
        conn = self.held_lock()
        try:
            worker, first = self.background(lambda: self.ask(c1, "app-1", q, key))
            wait_until(lambda: self.lock_waiters() >= 1, "request waiting on the lock", 10, 0.05)
            wait_until(lambda: self.run_row("kill", key), "its run recorded", 10, 0.05)
            self.apps["app-1"].stop(signal.SIGKILL)
        finally:
            conn.commit()
            conn.close()
        worker.join(30)
        try:
            while_leased = self.ask(c2, "app-2", q, key)
            wait_until(lambda: self.run_row("kill", key)["lease_expired"], "the lease expired",
                       LEASE_S + 15, 0.5)
            taken = self.ask(c2, "app-2", q, key)
            row = self.run_row("kill", key)
            turns = self.turns_of(row["conversation_id"])
            attempts = self.admin_sql("SELECT count(*) FROM app_conv.run_attempts "
                                      "WHERE run_id = %s", (row["run_id"],))[0][0]
        finally:
            self.start_app("app-1")
        replay = self.ask(c1, "app-1", q, key)
        same = (replay[0] == 200 and replay[1].get("replayed") is True
                and replay[1].get("run_id") == taken[1].get("run_id"))
        return {"ok": "error" in first and while_leased[0] == 409
                and (while_leased[1] or {}).get("detail", {}).get("code") == "same_request_running"
                and taken[0] == 200 and taken[1].get("status") == "answered"
                and row["status"] == "succeeded" and len(turns) == 1 and turns[0][1] == row["run_id"]
                and attempts == 2 and same,
                "killed_request": first.get("error"),
                "same_key_while_leased": [while_leased[0],
                                          (while_leased[1] or {}).get("detail", {}).get("code")],
                "taken_over": [taken[0], taken[1].get("status"), taken[1].get("replayed")],
                "run_status": row["status"], "turns_committed": len(turns),
                "attempts_counted": attempts, "replay_on_restarted_process_identical": same,
                "lease_s": LEASE_S}

    def s_overlapping_revisions(self) -> dict:
        """Two questions in one conversation on two processes: the second is
        refused while the first holds the conversation, then answered after
        it; the turns stay in order, one per run."""
        c1 = self.sign_in("conv", "app-1")
        c2 = c1.with_base(self.base("app-2"))
        status, body, _ = self.ask(c1, "app-1", "What is our total volume this quarter?")
        cid = body["conversation_id"]
        k2, k3 = (f"drill-conv-{secrets.token_hex(8)}" for _ in range(2))
        conn = self.held_lock()
        try:
            worker, second = self.background(lambda: self.ask(
                c1, "app-1", "What is our total volume last quarter?", k2, conversation_id=cid))
            wait_until(lambda: self.lock_waiters() >= 1, "second question waiting on the lock",
                       10, 0.05)
            third = self.ask(c2, "app-2", "total paid pack units last quarter", k3,
                             conversation_id=cid)
        finally:
            conn.commit()
            conn.close()
        worker.join(30)
        retried = self.ask(c2, "app-2", "total paid pack units last quarter", k3,
                           conversation_id=cid)
        turns = self.turns_of(cid)
        seqs, run_ids = [t[0] for t in turns], [t[1] for t in turns]
        revision = self.admin_sql("SELECT revision FROM app_conv.conversations "
                                  "WHERE conversation_id = %s", (cid,))[0][0]
        second_r = second.get("result") or (None, {}, {})
        return {"ok": status == 200 and second_r[0] == 200 and third[0] == 409
                and (third[1] or {}).get("detail", {}).get("code") == "conversation_busy"
                and retried[0] == 200 and len(turns) == 3 and seqs == sorted(set(seqs))
                and len(set(run_ids)) == 3,
                "first": status, "held": second_r[0],
                "other_process_while_held": [third[0], (third[1] or {}).get("detail", {}).get("code")],
                "other_process_after": retried[0], "turn_seqs": seqs,
                "distinct_runs": len(set(run_ids)), "conversation_revision": revision}

    def s_clarification_elsewhere(self) -> dict:
        """A clarification asked on one process and answered on the other:
        the paused thread resumes there, with the choice the user saw."""
        c1 = self.sign_in("clar", "app-1")
        c2 = c1.with_base(self.base("app-2"))
        status, first, _ = self.ask(c1, "app-1", f"What was the volume for {TWIN} in the last 3 months?")
        cid = (first or {}).get("conversation_id")
        status2, second, _ = self.ask(c2, "app-2", "the second one", conversation_id=cid,
                                      include_sql=True)
        rows = self.admin_sql("SELECT status, choices FROM app_conv.clarifications "
                              "WHERE conversation_id = %s", (cid,))
        threads = self.admin_sql("SELECT count(*) FROM app_graph.checkpoints WHERE thread_id LIKE %s",
                                 (f"{cid}.%",))[0][0]
        plan = json.dumps((second or {}).get("plan"))
        shown = [c.get("id") for c in (first or {}).get("choices") or []]
        # "the second one" is the second choice as shown, and only that one.
        order_kept = len(shown) == 2 and shown[1] in plan and shown[0] not in plan
        return {"ok": (first or {}).get("status") == "clarify" and status2 == 200
                and (second or {}).get("status") == "answered" and rows
                and rows[0][0] == "resolved" and threads == 0 and order_kept,
                "asked_on": "app-1", "answered_on": "app-2",
                "statuses": [(first or {}).get("status"), (second or {}).get("status")],
                "clarification": rows[0][0] if rows else None,
                "second_choice_bound": order_kept, "checkpoints_left": threads}

    def s_revocation(self) -> dict:
        """Pricing revoked between an answer and its replay on the other
        process: the replay is withheld, the history no longer shows the
        figure, and a new question is answered under the new access."""
        q = "What is our total revenue this quarter?"
        c1 = self.sign_in("rev", "app-1")
        c2 = c1.with_base(self.base("app-2"))
        key = f"drill-rev-{secrets.token_hex(8)}"
        status, body, _ = self.ask(c1, "app-1", q, key)
        figure = re.findall(r"\$[\d,]+(?:\.\d+)?", json.dumps(body))
        self.admin_sql("UPDATE users SET can_view_wac = 0 WHERE user_id = %s",
                       (self.users["rev"]["user_id"],))
        replay = self.ask(c2, "app-2", q, key)
        h_status, history, _ = c2.call("GET", f"/api/conversations/{body['conversation_id']}")
        fresh = self.ask(c2, "app-2", q)
        leaked = [name for name, r in (("replay", replay[1]), ("history", history),
                                       ("new question", fresh[1])) if "$" in json.dumps(r)]
        return {"ok": status == 200 and bool(figure) and replay[0] == 403
                and (replay[1] or {}).get("detail", {}).get("code") == "access_changed"
                and not leaked,
                "before": [status, "figure shown" if figure else "no figure"],
                "replay_after_revocation": [replay[0], (replay[1] or {}).get("detail", {}).get("code")],
                "history_status": h_status, "new_question": [fresh[0], (fresh[1] or {}).get("status")],
                "figure_seen_after_revocation_in": leaked}

    def s_quota_after_deletion(self) -> dict:
        """The per-minute limit counts attempts, not conversations: deleting
        every conversation refunds nothing, on either process."""
        c1 = self.sign_in("quota", "app-1")
        clients = {"app-1": c1, "app-2": c1.with_base(self.base("app-2"))}
        conversations, codes = [], []
        for i in range(20):
            app = "app-1" if i % 2 == 0 else "app-2"
            status, body, _ = self.ask(clients[app], app, "What is our total volume this quarter?")
            codes.append(status)
            conversations.append((body or {}).get("conversation_id"))
        deleted = [clients["app-2" if i % 2 == 0 else "app-1"].call(
            "DELETE", f"/api/conversations/{cid}")[0] for i, cid in enumerate(conversations)]
        left = self.admin_sql("SELECT count(*) FROM app_conv.conversations WHERE owner_user_id = %s",
                              (self.users["quota"]["user_id"],))[0][0]
        after, body, headers = self.ask(clients["app-1"], "app-1", "What is our total volume this quarter?")
        return {"ok": codes == [200] * 20 and deleted == [200] * 20 and left == 0 and after == 429,
                "answered": codes.count(200), "deleted": deleted.count(200),
                "conversations_left": left, "after_deletion": after,
                "retry_after": {k.lower(): v for k, v in headers.items()}.get("retry-after")}

    def s_overload_and_cancel(self) -> dict:
        """A third process with one query slot and one queue place. One
        question runs (held by the lock), one waits, and one more is refused
        at once; the waiting one is cancelled from another process. Nothing
        is committed for either; the refused one, retried, is answered."""
        q = "What is our total volume this quarter?"
        self.start_app("app-3", {"PAC_ADMISSION_QUERY_SLOTS": "1", "PAC_ADMISSION_QUERY_QUEUE": "1",
                                 "PAC_ADMISSION_QUERY_WAIT_SECONDS": "45",
                                 "PAC_REQUEST_DEADLINE_SECONDS": "60",
                                 "PAC_RUN_LEASE_SECONDS": "120",
                                 "PAC_STATEMENT_TIMEOUT_MS": "60000"})
        try:
            return self._overload_and_cancel(q)
        finally:
            if "app-3" in self.apps:
                self.stop_app("app-3")

    def _overload_and_cancel(self, q: str) -> dict:
        users = ["o0", "o1", "o2"]
        clients = {u: self.sign_in(u, "app-3") for u in users}
        keys = {u: f"drill-{u}-{secrets.token_hex(8)}" for u in users}
        conn = self.held_lock()
        try:
            running, r_out = self.background(lambda: self.ask(clients["o0"], "app-3", q, keys["o0"]))
            wait_until(lambda: self.lock_waiters() >= 1, "one question running", 10, 0.05)
            # Two more at once: one takes the queue place, the other is refused.
            pending = {u: self.background(lambda u=u: self.ask(clients[u], "app-3", q, keys[u]))
                       for u in ("o1", "o2")}
            refused_user = wait_until(lambda: next(
                (u for u, (_, out) in pending.items() if "result" in out), None),
                "one refused at once", 10, 0.02)
            queued_user = next(u for u in pending if u != refused_user)
            refused = pending[refused_user][1]
            # Cancelled by its owner from another process, by its key.
            other = clients[queued_user].with_base(self.base("app-1"))
            cancel = other.call("POST", "/api/runs/cancel", {"idempotency_key": keys[queued_user]})
            pending[queued_user][0].join(10)
            queued = pending[queued_user][1]
        finally:
            conn.commit()
            conn.close()
        running.join(60)
        retried = self.ask(clients[refused_user], "app-3", q, keys[refused_user])
        rows = {u: self.run_row(u, keys[u]) for u in users}
        turns = {u: len(self.turns_of(rows[u]["conversation_id"])) for u in users if rows[u]}
        rr = refused["result"]
        q_result = queued.get("result") or (None, {}, {})
        return {"ok": rr[0] == 503 and (rr[1] or {}).get("detail", {}).get("code") == "overloaded"
                and {k.lower() for k in rr[2]} >= {"retry-after"} and refused["took_s"] < 2
                and cancel[0] == 200 and q_result[0] == 200
                and (q_result[1] or {}).get("status") == "cancelled"
                and rows[queued_user]["status"] == "cancelled" and turns[queued_user] == 0
                and r_out["result"][0] == 200 and retried[0] == 200
                and retried[1].get("status") == "answered" and turns[refused_user] == 1,
                "refused": [rr[0], (rr[1] or {}).get("detail", {}).get("code"), refused["took_s"]],
                "cancel_from_other_process": cancel[0],
                "queued_outcome": [q_result[0], (q_result[1] or {}).get("status"), queued.get("took_s")],
                "running_outcome": r_out["result"][0], "refused_retried": [retried[0],
                                                                          retried[1].get("status")],
                "run_status": {("running", "queued", "refused")[i]: rows[u]["status"]
                               for i, u in enumerate(["o0", queued_user, refused_user])},
                "turns_committed": {("running", "queued", "refused")[i]: turns[u]
                                    for i, u in enumerate(["o0", queued_user, refused_user])}}

    def s_missing_metrics(self) -> dict:
        """Every process that reports freshness stops: the absence must page."""
        for name in ("app-1", "app-2"):
            self.apps[name].stop()
        fired = self.wait_alert("FreshnessNotReported", "firing", 120)
        self.start_apps()                  # new instances: their totals start again
        cleared = self.wait_alert("FreshnessNotReported", "inactive", 120)
        return {"fired_after_s": fired, "cleared_after_s": cleared}

    def s_collector_outage(self) -> dict:
        """Kill the collector; answers continue, latency and memory stay
        bounded; the scrape failure pages; after a restart it clears and the
        running totals catch up."""
        workers = [self.sign_in(f"w{i}", "app-1") for i in range(6)]
        pairs = [(w, "app-1") if i % 2 == 0 else (w.with_base(self.base("app-2")), "app-2")
                 for i, w in enumerate(workers)]

        def burst(n: int) -> list[float]:
            took = []
            for i in range(n):
                c, app = pairs[i % len(pairs)]
                t0 = time.monotonic()
                status, body, _ = self.ask(c, app, "What is our total volume this quarter?")
                took.append(time.monotonic() - t0)
                if status != 200 or body.get("status") != "answered":
                    raise RuntimeError(f"an answer failed during the outage: {status}")
            return took

        baseline = burst(10)
        rss_before = {n: p.rss_kib() for n, p in self.apps.items()}
        self.collector.stop(signal.SIGKILL)
        outage_started = time.monotonic()
        fired = self.wait_alert("TelemetryPipelineDown", "firing", 60)
        during = burst(40)
        # Hold the outage across several export intervals of both processes.
        wait_until(lambda: time.monotonic() - outage_started >= 35, "outage spans two exports", 60, 1)
        during += burst(10)
        rss_after = {n: p.rss_kib() for n, p in self.apps.items()}
        export_failures = sum(len(re.findall(r'"event": "telemetry\.(?:metrics|spans)_dropped"',
                                             p.log.read_text(errors="replace")))
                              for p in self.apps.values())
        # A process stopped while the collector is down: the final flush is
        # bounded (2 x PAC_OTEL_TIMEOUT_S + 1 = 11 s), so the exit is too.
        # uvicorn re-raises SIGTERM after its graceful shutdown, so a clean
        # exit reports -15; a forced one (Proc.stop's SIGKILL) would be -9.
        t0 = time.monotonic()
        exit_code = self.apps["app-2"].stop(signal.SIGTERM, timeout=60)
        shutdown_s = round(time.monotonic() - t0, 1)
        self.restart_collector()
        cleared = self.wait_alert("TelemetryPipelineDown", "inactive", 60)
        self.start_app("app-2")
        totals_ok = self.totals_match(timeout=60)
        p95 = lambda xs: statistics.quantiles(xs, n=20)[-1]        # noqa: E731
        growth = {n: (rss_after[n] or 0) - (rss_before[n] or 0) for n in rss_before}
        return {"ok": all(p.alive() for p in self.apps.values()) and totals_ok
                and p95(during) < p95(baseline) + 1.0 and max(growth.values()) < 64 * 1024
                and export_failures >= 1 and exit_code in (0, -signal.SIGTERM)
                and shutdown_s < 20,
                "answers_during_outage": len(during), "p95_s_baseline": round(p95(baseline), 3),
                "p95_s_during": round(p95(during), 3), "rss_growth_kib": growth,
                "export_failure_log_lines": export_failures,
                "stopped_during_outage": {"exit_code": exit_code, "took_s": shutdown_s},
                "TelemetryPipelineDown_fired_after_s": fired,
                "TelemetryPipelineDown_cleared_after_s": cleared,
                "totals_caught_up": totals_ok}

    def s_alerts_clear(self) -> dict:
        """Every alert that fired is inactive once its window has passed."""
        fired = ["AuditLoss", "QueryTimeouts", "Errors"]
        cleared = {name: self.wait_alert(name, "inactive", 150) for name in fired}
        return {"cleared_after_s": cleared}

    def s_dashboard(self) -> dict:
        """Every dashboard query is valid PromQL against the live backend;
        those whose metrics the drill exercised return data."""
        dash = json.loads((OBS / "dashboard.json").read_text())
        empty, invalid = [], []
        for panel in dash["panels"]:
            for target in panel["targets"]:
                try:
                    if not self.prom.query(target["expr"]):
                        empty.append(target["expr"])
                except Exception:
                    invalid.append(target["expr"])
        unexercised = re.compile(r"pac_llm_|pac_admission_")
        unexpected_empty = [e for e in empty if not unexercised.search(e)]
        return {"ok": not invalid and not unexpected_empty, "invalid": invalid,
                "empty": empty, "unexpected_empty": unexpected_empty,
                "queries": sum(len(p["targets"]) for p in dash["panels"])}

    def s_sentinels(self) -> dict:
        """Nothing the drill sent in reaches telemetry or logs. The positive
        control: the sentinel question is in the conversation store."""
        for name in ("app-1", "app-2"):
            self.apps[name].stop()                        # flushes telemetry
        self.collector.stop(signal.SIGTERM)               # flushes the file exporter
        stored = self.admin_sql("SELECT count(*) FROM app_conv.turns WHERE question LIKE %s",
                                (f"%{self.sentinels['question']}%",))[0][0]
        values = {k: v for k, v in self.sentinels.items() if v}
        cookies = [c for c in values.pop("cookies", "").split("|") if len(c) >= 16]
        needles = {**{k: v for k, v in values.items()},
                   **{f"cookie{i}": c for i, c in enumerate(cookies)},
                   **{f"db_password_{k.lower()}": v for k, v in self.db_pw.items()},
                   "admin_password": self.admin_pw}
        for u in self.users.values():
            needles[f"password_{u['user_id']}"] = u["password"]
            needles[f"email_{u['user_id']}"] = u["email"]
        haystacks = {p.name: p.read_text(errors="replace")
                     for p in list((self.work / "otel").glob("*.jsonl")) + list(self.logs.glob("*.log"))}
        labels = self.prom.get("/api/v1/labels")
        label_values = []
        for label in labels:
            label_values += self.prom.get(f"/api/v1/label/{label}/values")
        haystacks["prometheus label values"] = "\n".join(label_values)
        found = sorted({(k, h) for k, v in needles.items() for h, text in haystacks.items()
                        if v in text})
        sizes = {h: len(t) for h, t in haystacks.items()}
        return {"ok": stored >= 1 and not found and sizes.get("traces.jsonl", 0) > 0
                and sizes.get("metrics.jsonl", 0) > 0,
                "positive_control_turns_with_sentinel": stored,
                "sentinels_checked": sorted(needles.keys() - {k for k in needles
                                                              if k.startswith(("password_", "cookie",
                                                                               "db_password", "admin"))})
                + [f"{len(cookies)} session cookies", "database and user passwords"],
                "leaks": [f"{k} in {h}" for k, h in found], "bytes_scanned": sizes}

    def span_errors_by_run(self) -> dict[str, set[str]]:
        """run id -> the error types recorded on any span of its traces,
        from the collector's file export (OTLP JSON lines)."""
        runs_of, errors_of = {}, {}
        path = self.work / "otel" / "traces.jsonl"
        for raw in path.read_text(errors="replace").splitlines():
            try:
                doc = json.loads(raw)
            except ValueError:
                continue
            for rs in doc.get("resourceSpans", []):
                for ss in rs.get("scopeSpans", []):
                    for span in ss.get("spans", []):
                        trace = span.get("traceId")
                        attrs = {a["key"]: next(iter(a.get("value", {}).values()), None)
                                 for a in span.get("attributes", [])}
                        if attrs.get("pac.run_id"):
                            runs_of.setdefault(trace, set()).add(attrs["pac.run_id"])
                        kinds = errors_of.setdefault(trace, set())
                        if (span.get("status") or {}).get("message"):
                            kinds.add(span["status"]["message"])
                        for event in span.get("events", []):
                            for a in event.get("attributes", []):
                                if a["key"] == "exception.type":
                                    kinds.add(next(iter(a.get("value", {}).values()), ""))
        out: dict[str, set[str]] = {}
        for trace, run_ids in runs_of.items():
            for run_id in run_ids:
                out.setdefault(run_id, set()).update(errors_of.get(trace, set()))
        return out

    def s_logs(self) -> dict:
        """What diagnosis needs survives the redaction: one JSON object a
        line, a stable event code on every line, exception types from the
        allowlist, and ids that lead from a response (X-Request-ID) to its
        log line, and from that line (run_id) to its trace and the error
        type the trace recorded."""
        sys.path.insert(0, str(ROOT))
        import psycopg.errors

        from app.logs import ERROR_TYPES, EVENTS
        known = set(EVENTS.values()) | {"http.access", "server.started", "log.external"}
        lines, not_json = [], 0
        for path in sorted(self.logs.glob("app-*.log")):
            for raw in path.read_text(errors="replace").splitlines():
                if raw.strip():
                    try:
                        lines.append(json.loads(raw))
                    except ValueError:
                        not_json += 1
        events: dict[str, int] = {}
        for line in lines:
            events[str(line.get("event"))] = events.get(str(line.get("event")), 0) + 1
        unknown = sorted(e for e in events if e not in known)
        error_types = sorted({line["error"]["type"] for line in lines
                              if isinstance(line.get("error"), dict)})
        # The driver's own classes are named after their SQLSTATE condition.
        driver = {n for n in error_types if isinstance(getattr(psycopg.errors, n, None), type)}
        # The refused audit insert (s_audit_loss) must say what kind of failure it was.
        audit_types = sorted({(line.get("error") or {}).get("type") for line in lines
                              if line.get("event") == "audit.write_failed"})
        failed = [line for line in lines if line.get("event") == "query.failed"]
        responded = set(self.response_ids)
        spans = self.span_errors_by_run()
        to_response = [f for f in failed if f.get("http_id") in responded]
        to_trace = [f for f in failed if f.get("request_id") and "QueryCanceled" in spans.get(
            f.get("run_id"), set())]
        return {"ok": bool(lines) and not_json == 0 and not unknown
                and set(error_types) <= ERROR_TYPES | {"Exception"} | driver
                and audit_types == ["InsufficientPrivilege"]
                and len(failed) >= 12 and len(to_response) == len(failed)
                and len(to_trace) == len(failed),
                "lines": len(lines), "not_json": not_json, "events": events,
                "unknown_events": unknown, "error_types": error_types,
                "audit_write_failed_types": audit_types,
                "query_failed_lines": len(failed),
                "linked_to_the_response_id": len(to_response),
                "linked_to_a_trace_recording_QueryCanceled": len(to_trace)}

    # -- run ----------------------------------------------------------------------------

    def run(self) -> int:
        try:
            self.start_postgres()
            self.bootstrap()
            self.make_users()
            self.add_twins()
            self.start_collector()
            self.start_prometheus()
            self.start_apps()
            self.attempt("traffic of every outcome on two processes, counted per process",
                         self.s_traffic)
            self.attempt("a per-user rate limit holds across processes", self.s_rate_limit)
            self.attempt("one idempotency key on two processes: refused, then replayed",
                         self.s_duplicate_key)
            self.attempt("a refused audit insert pages AuditLoss", self.s_audit_loss)
            self.attempt("a burst of statement timeouts pages QueryTimeouts and Errors",
                         self.s_timeouts)
            self.attempt("a malformed batch, old events, a stopped feed and a resumed one",
                         self.s_feed)
            self.attempt("both processes answer from the refreshed snapshot",
                         self.s_refresh_seen_by_both)
            self.attempt("a process killed mid-request: the other takes the key over after "
                         "the lease and commits once", self.s_worker_killed)
            self.attempt("one conversation on two processes: the second question waits its "
                         "turn", self.s_overlapping_revisions)
            self.attempt("a clarification asked on one process is answered on the other",
                         self.s_clarification_elsewhere)
            self.attempt("pricing revoked: the replay on the other process is withheld, "
                         "history and new answers show no figure", self.s_revocation)
            self.attempt("deleting conversations refunds no rate, on either process",
                         self.s_quota_after_deletion)
            self.attempt("overload refused at once; a queued request cancelled from another "
                         "process", self.s_overload_and_cancel)
            self.attempt("both processes stopped: FreshnessNotReported pages, then clears",
                         self.s_missing_metrics)
            self.attempt("the collector killed: answers continue, the outage pages, totals "
                         "catch up", self.s_collector_outage)
            self.attempt("every alert that fired clears", self.s_alerts_clear)
            self.attempt("every dashboard query runs against the backend", self.s_dashboard)
            self.attempt("no sentinel reaches telemetry, Prometheus or a log", self.s_sentinels)
            self.attempt("logs keep event codes, error types and ids that lead to the trace",
                         self.s_logs)
        except Exception as exc:
            self.record("drill setup", False, error=f"{type(exc).__name__}: {str(exc)[:500]}")
        return 0 if self.results and all(r["status"] == "passed" for r in self.results) else 1

    def teardown(self, keep: bool) -> None:
        for p in reversed(self.procs):
            p.stop(signal.SIGTERM, 20)
        if hasattr(self, "pg_data"):
            subprocess.run(["pg_ctl", "-D", str(self.pg_data), "-m", "fast", "-w", "stop"],
                           capture_output=True)
        if not keep:
            import shutil
            shutil.rmtree(self.work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tools", type=pathlib.Path, required=True,
                    help="the directory scripts/fetch_ops_tools.sh filled")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--keep", action="store_true", help="keep the work directory (logs, data)")
    args = ap.parse_args()
    work = pathlib.Path(tempfile.mkdtemp(prefix="pac-ops-drill-"))
    drill = Drill(args.tools.resolve(), work)
    code = 1
    try:
        code = drill.run()
    finally:
        tools = {"otelcol-contrib": subprocess.run(
                     [str(drill.tools / "otelcol" / "otelcol-contrib"), "--version"],
                     capture_output=True, text=True).stdout.strip(),
                 "prometheus": subprocess.run(
                     [str(drill.tools / "prometheus" / "prometheus"), "--version"],
                     capture_output=True, text=True).stdout.splitlines()[0],
                 "postgres": subprocess.run(["postgres", "--version"], capture_output=True,
                                            text=True).stdout.strip()}
        drill.teardown(args.keep)
        summary = {
            "drill_version": DRILL_VERSION,
            "label": "local evidence: one machine, one PostgreSQL cluster created for the drill, "
                     "two application processes and a third for overload (not containers), a "
                     "local collector and Prometheus with shortened alert windows and a "
                     f"{LEASE_S} s run lease; not a hosted backend or an on-call incident drill",
            "tools": tools,
            "alerts_not_exercised": NOT_EXERCISED,
            "drill_rule_substitutions": {k: v["sub"] for k, v in DRILL_RULES.items()},
            "shortened_settings": {"PAC_RUN_LEASE_SECONDS": LEASE_S,
                                   "PAC_REQUEST_DEADLINE_SECONDS": DEADLINE_S},
            "passed": sum(r["status"] == "passed" for r in drill.results),
            "failed": sum(r["status"] == "failed" for r in drill.results),
            "results": drill.results,
            "work_dir_kept": str(work) if args.keep else None,
        }
        args.out.write_text(json.dumps(summary, indent=1, default=str) + "\n")
        print(f"drill: {summary['passed']} passed, {summary['failed']} failed -> {args.out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
