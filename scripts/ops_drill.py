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
* the collector (deploy/observability/otel-collector.yaml) and Prometheus
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
killed and restarted while answers continue. Each expected alert must fire,
and later clear. At the end every exported trace and metric, every Prometheus
label value and every log is searched for sentinel values the drill sent in
(a question, a key, an email, territory names, a revenue figure, a session
cookie, the database passwords).

This is local evidence: one machine, one PostgreSQL, processes not
containers, shortened alert windows. It is not a hosted collector, a hosted
backend, an incident drill with on-call, or a load test.
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
DRILL_VERSION = "1.0.0"

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
                                          *((f"w{i}", "exec", 0, None, None) for i in range(6))):
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

    def start_collector(self) -> None:
        self.otlp_port, self.prom_exp_port = free_port(), free_port()
        (self.work / "otel").mkdir(exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.work),
               "PAC_OTEL_RECEIVER": f"127.0.0.1:{self.otlp_port}",
               "PAC_OTEL_PROMETHEUS": f"127.0.0.1:{self.prom_exp_port}",
               "PAC_OTEL_FILE_DIR": str(self.work / "otel"),
               "PAC_OTEL_METRIC_EXPIRATION": "20s"}
        self.collector = Proc("otel-collector", [str(self.tools / "otelcol" / "otelcol-contrib"),
                                                 "--config", str(OBS / "otel-collector.yaml")],
                              env, self.work, self.logs).start()
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
                "PAC_COOKIE_SECURE": "false", "PAC_OTEL_SOURCE_NAMES": "drill-feed"}

    def start_apps(self) -> None:
        self.apps: dict[str, Proc] = {}
        self.app_ports = getattr(self, "app_ports", {"app-1": free_port(), "app-2": free_port()})
        for name, port in self.app_ports.items():
            proc = Proc(name, [sys.executable, "-m", "uvicorn", "app.api.main:app",
                               "--host", "127.0.0.1", "--port", str(port),
                               "--timeout-graceful-shutdown", "20",
                               "--log-config", str(ROOT / "app" / "log_config.json")],
                        self.app_env(), self.work, self.logs).start()
            self.apps[name] = proc
            self.procs.append(proc)
        for name, port in self.app_ports.items():
            wait_until(lambda port=port: urllib.request.urlopen(
                f"http://127.0.0.1:{port}/ready", timeout=2).status == 200, f"{name} ready", 120)

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
            timeout: float = 90) -> tuple[int, Any, dict]:
        with self.lock:
            self.asks[app] += 1
        return client.call("POST", "/api/ask", {"question": question},
                           headers={"Idempotency-Key": key} if key else None, timeout=timeout)

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

    def s_missing_metrics(self) -> dict:
        """Every process that reports freshness stops: the absence must page."""
        for name in ("app-1", "app-2"):
            self.apps[name].stop()
        fired = self.wait_alert("FreshnessNotReported", "firing", 120)
        self.start_apps()
        # New processes, new instances: their running totals start again.
        with self.lock:
            self.asks = {name: 0 for name in self.asks}
        cleared = self.wait_alert("FreshnessNotReported", "inactive", 120)
        # Restarted processes are new instances with new running totals.
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
        self.restart_collector()
        cleared = self.wait_alert("TelemetryPipelineDown", "inactive", 60)
        totals_ok = self.totals_match(timeout=60)
        p95 = lambda xs: statistics.quantiles(xs, n=20)[-1]        # noqa: E731
        growth = {n: (rss_after[n] or 0) - (rss_before[n] or 0) for n in rss_before}
        return {"ok": all(p.alive() for p in self.apps.values()) and totals_ok
                and p95(during) < p95(baseline) + 1.0 and max(growth.values()) < 64 * 1024
                and export_failures >= 1,
                "answers_during_outage": len(during), "p95_s_baseline": round(p95(baseline), 3),
                "p95_s_during": round(p95(during), 3), "rss_growth_kib": growth,
                "export_failure_log_lines": export_failures,
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

    # -- run ----------------------------------------------------------------------------

    def run(self) -> int:
        try:
            self.start_postgres()
            self.bootstrap()
            self.make_users()
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
            self.attempt("both processes stopped: FreshnessNotReported pages, then clears",
                         self.s_missing_metrics)
            self.attempt("the collector killed: answers continue, the outage pages, totals "
                         "catch up", self.s_collector_outage)
            self.attempt("every alert that fired clears", self.s_alerts_clear)
            self.attempt("every dashboard query runs against the backend", self.s_dashboard)
            self.attempt("no sentinel reaches telemetry, Prometheus or a log", self.s_sentinels)
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
                     "two application processes (not containers), a local collector and "
                     "Prometheus with shortened alert windows; not a hosted backend or an "
                     "on-call incident drill",
            "tools": tools,
            "alerts_not_exercised": NOT_EXERCISED,
            "drill_rule_substitutions": {k: v["sub"] for k, v in DRILL_RULES.items()},
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
