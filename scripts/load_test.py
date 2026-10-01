#!/usr/bin/env python3
"""A reproducible load profile, on a DISPOSABLE full-scale copy.

Starts the application the way the image does (uvicorn, two workers),
provisions throwaway users of each role, and drives real HTTP sessions
through POST /api/ask in a closed loop at rising concurrency. Then, under
steady load, publishes data twice -- a batch inside the latest week, and one
that opens a new week and rewrites every offset -- to measure refresh
contention. Prints one JSON document.

    createdb -T pharma_analytics -O pac_owner pharma_analytics_ingestscale
    PAC_DB_NAME=pharma_analytics_ingestscale python3 scripts/load_test.py

It refuses the working database. Planning is the offline planner: this
measures the system around the model, not the model. Per-user quotas are
lifted for the run (each virtual user would otherwise be rate-limited, which
is the quota working, not capacity); that is the only setting changed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROTECTED = {"pharma_analytics"}
PORT = 8020

#: Question classes by cost, as measured on the full dataset: cheap ones are
#: one aggregate; expensive ones scan by facility and month or go through
#: the dense time series under row-level security.
QUESTIONS = {
    "cheap": ["total paid pack units last quarter", "paid pack units this month",
              "top 10 accounts by paid pack units last quarter"],
    "medium": ["paid pack units by product last 6 months", "volume growth by account last quarter",
               "top 20 facilities by paid pack units last 6 months",
               "market share by territory last quarter"],
    "expensive": ["paid pack units by month for the last 6 months",
                  "paid pack units by facility and month, all time"],
}
CLASS_WEIGHTS = {"cheap": 0.45, "medium": 0.4, "expensive": 0.15}
#: A sales organisation: many representatives, fewer directors, few executives.
ROLE_WEIGHTS = {"ram": 0.6, "director": 0.3, "exec": 0.1}


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return round(ordered[k], 1)


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95),
            "p99": pct(values, 99), "max": round(max(values), 1) if values else None}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def provision() -> list[dict]:
    from app.auth.identity import set_credential
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute(
            "SELECT z.territory_name, z.region_name, count(*) AS n FROM sales s "
            "JOIN organizations o USING (org_id) JOIN zip_territory z USING (zip) "
            "WHERE s.mo_offset < 3 GROUP BY 1, 2 ORDER BY n DESC LIMIT 6")
        territories = [dict(r) for r in cur.fetchall()]
    regions = list(dict.fromkeys(t["region_name"] for t in territories))[:4]
    plan = ([("ram", t["territory_name"], None) for t in territories]
            + [("director", None, r) for r in regions]
            + [("exec", None, None)] * 2)
    users = []
    for role, territory, region in plan:
        suffix = secrets.token_hex(4)
        user_id, password = f"pacload-{role}-{suffix}", secrets.token_urlsafe(18)
        with owner_transaction() as cur:
            cur.execute(
                "INSERT INTO users (user_id, email, full_name, role, territory_name, "
                "region_name, can_view_wac) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (user_id, f"{user_id}@load.invalid", f"Load {role}", role, territory, region,
                 1 if role == "exec" else 0))
        set_credential(user_id, password)
        users.append({"user_id": user_id, "email": f"{user_id}@load.invalid",
                      "password": password, "role": role})
    return users


def remove(users: list[dict]) -> None:
    from app.db import owner_transaction

    ids = [u["user_id"] for u in users]
    with owner_transaction() as cur:
        cur.execute("DELETE FROM app_conv.conversations WHERE owner_user_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM app_conv.runs WHERE owner_user_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM app_auth.sessions WHERE user_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM app_auth.credentials WHERE user_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM app_meta.query_audit WHERE user_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM users WHERE user_id = ANY(%s)", (ids,))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def start_server(db: str) -> subprocess.Popen:
    import urllib.request

    env = {**os.environ, "PAC_DB_NAME": db, "PAC_COOKIE_SECURE": "false",
           "PAC_LLM_PROVIDER": "offline", "PAC_ENVIRONMENT": "local",
           "PAC_USER_REQUESTS_PER_MINUTE": "1000000", "PAC_USER_REQUESTS_PER_HOUR": "10000000",
           "PAC_USER_CONCURRENT_RUNS": "1000", "AWS_EC2_METADATA_DISABLED": "true",
           "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--workers", "2", "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ready", timeout=2).status == 200:
                return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("server did not become ready")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.rows: list[dict] = []
        self.lock = threading.Lock()

    def add(self, row: dict) -> None:
        with self.lock:
            self.rows.append(row)


def virtual_user(user: dict, stop: threading.Event, rec: Recorder, phase: str, seed: int):
    import httpx2

    rng = random.Random(seed)
    client = httpx2.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=120)
    r = client.post("/api/login", json={"email": user["email"], "password": user["password"]})
    if r.status_code != 200:
        rec.add({"phase": phase, "role": user["role"], "error": f"login {r.status_code}"})
        return
    classes, weights = zip(*CLASS_WEIGHTS.items())
    while not stop.is_set():
        cls = rng.choices(classes, weights)[0]
        question = rng.choice(QUESTIONS[cls])
        started = time.time()
        t0 = time.perf_counter()
        try:
            r = client.post("/api/ask", json={"question": question},
                            headers={"Idempotency-Key": f"load-{uuid.uuid4().hex}"})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") \
                else {}
            detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
            rec.add({"phase": phase, "role": user["role"], "class": cls,
                     "started": started, "ms": (time.perf_counter() - t0) * 1000,
                     "http": r.status_code, "status": body.get("status"),
                     "code": detail.get("code"),
                     "request_id": body.get("request_id"),
                     # The first words of a non-answer: which failure it was.
                     "message": (body.get("message") or "")[:40]
                     if body.get("status") != "answered" else None})
            if r.status_code == 503 and detail.get("code") == "overloaded":
                # As the interface does: wait as asked, with jitter.
                time.sleep(min(float(r.headers.get("Retry-After", 2)), 10)
                           * (1 + rng.random() * 0.5))
        except Exception as exc:
            rec.add({"phase": phase, "role": user["role"], "class": cls, "started": started,
                     "ms": (time.perf_counter() - t0) * 1000, "http": None,
                     "status": None, "error": type(exc).__name__})
    client.close()


def users_for(n: int, pool: list[dict], rng: random.Random) -> list[dict]:
    by_role = {r: [u for u in pool if u["role"] == r] for r in ROLE_WEIGHTS}
    roles, weights = zip(*ROLE_WEIGHTS.items())
    return [rng.choice(by_role[rng.choices(roles, weights)[0]]) for _ in range(n)]


def run_phase(phase: str, n: int, seconds: float, pool: list[dict], rec: Recorder,
              during=None) -> dict:
    rng = random.Random(hash(phase) & 0xFFFF)
    stop = threading.Event()
    threads = [threading.Thread(target=virtual_user, args=(u, stop, rec, phase, i))
               for i, u in enumerate(users_for(n, pool, rng))]
    for t in threads:
        t.start()
    window = None
    started = time.time()
    if during is not None:
        time.sleep(min(10, seconds / 4))
        w0 = time.time()
        extra = during()
        window = {"start": w0, "end": time.time(), **(extra or {})}
    remaining = seconds - (time.time() - started)
    if remaining > 0:
        time.sleep(remaining)
    stop.set()
    for t in threads:
        t.join(180)
    return {"phase": phase, "concurrency": n, "wall_s": round(time.time() - started, 1),
            "window": window}


class Sampler(threading.Thread):
    """Connections per login role, from pg_stat_activity, twice a second."""

    def __init__(self, db: str):
        super().__init__(daemon=True)
        self.db, self.stop, self.peak, self.active_peak = db, threading.Event(), {}, {}
        self.visible = True

    def run(self):
        import psycopg
        try:
            conn = psycopg.connect(dbname=self.db, autocommit=True)
        except Exception:
            self.visible = False
            return
        with conn:
            while not self.stop.is_set():
                rows = conn.execute(
                    "SELECT usename, count(*) AS n, count(*) FILTER (WHERE state = 'active') "
                    "FROM pg_stat_activity WHERE datname = %s GROUP BY 1", (self.db,)).fetchall()
                for user, n, active in rows:
                    if user:
                        self.peak[user] = max(self.peak.get(user, 0), n)
                        self.active_peak[user] = max(self.active_peak.get(user, 0), active)
                time.sleep(0.5)


def publish(kind: str, db_week_offset_days: int):
    """An ingestion batch, published while the load runs."""
    from app.data.ingest import ingest
    from app.data.sources import SyntheticIncrementalSource
    from app.db import owner_transaction

    def go():
        with owner_transaction() as cur:
            cur.execute("SELECT org_id FROM organizations WHERE org_status = 'Active' "
                        "ORDER BY 1 LIMIT 500")
            orgs = [r["org_id"] for r in cur.fetchall()]
            cur.execute("SELECT ndc FROM products ORDER BY 1")
            ndcs = [r["ndc"] for r in cur.fetchall()]
            cur.execute("SELECT max(week_ending_date) AS we FROM app_ref.calendar")
            latest = datetime.fromisoformat(cur.fetchone()["we"]).replace(
                hour=16, tzinfo=timezone.utc)
        anchor = latest + timedelta(days=db_week_offset_days)
        batch = SyntheticIncrementalSource(orgs=orgs, ndcs=ndcs, latest_week_ending=anchor,
                                           seed=random.randint(1, 10_000)).new_sales(
            f"load-{kind}-{secrets.token_hex(3)}", 500)
        t0 = time.perf_counter()
        out = ingest(batch, now=anchor + timedelta(days=4))
        return {"batch": kind, "publish_s": round(time.perf_counter() - t0, 1),
                "status": out.status, "anchor_shift_weeks": out.anchor_shift_weeks,
                "dead_rows_after_reclaim": out.dead_rows_after_reclaim}
    return go


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def server_times(request_ids: list[str]) -> dict[str, dict]:
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT request_id, total_ms, db_ms, status FROM app_meta.query_audit "
                    "WHERE request_id = ANY(%s)", (request_ids,))
        return {r["request_id"]: dict(r) for r in cur.fetchall()}


def _tally(items) -> list[dict]:
    counts: dict[tuple, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return [{"audit_status": k[0], "class": k[1], "message": k[2], "n": n}
            for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def report(rows: list[dict], phases: list[dict]) -> list[dict]:
    audit = server_times([r["request_id"] for r in rows if r.get("request_id")])
    out = []
    for ph in phases:
        mine = [r for r in rows if r.get("phase") == ph["phase"]]
        ok = [r for r in mine if r.get("http") == 200 and r.get("status") == "answered"]
        # A refusal for load is a predictable outcome, counted on its own;
        # an error is anything else that did not answer.
        refused = [r for r in mine if r.get("code") == "overloaded"]
        failed = [r for r in mine if r.get("code") != "overloaded"
                  and (r.get("http") != 200 or r.get("status") == "error")]
        statuses: dict[str, int] = {}
        for r in mine:
            key = r.get("status") or f"http_{r.get('http')}" if r.get("http") else (
                r.get("error") or "none")
            statuses[str(key)] = statuses.get(str(key), 0) + 1
        server = [audit[r["request_id"]] for r in ok if r.get("request_id") in audit]
        entry = {
            **ph,
            "requests": len(mine),
            "throughput_rps": round(len(mine) / ph["wall_s"], 2) if ph["wall_s"] else None,
            "answers_per_s": round(len(ok) / ph["wall_s"], 2) if ph["wall_s"] else None,
            "error_rate": round(len(failed) / len(mine), 4) if mine else None,
            "refused_rate": round(len(refused) / len(mine), 4) if mine else None,
            "statuses": statuses,
            "client_ms": summary([r["ms"] for r in ok]),
            "server_total_ms": summary([s["total_ms"] for s in server if s["total_ms"]]),
            "server_db_ms": summary([s["db_ms"] for s in server if s["db_ms"]]),
            "client_ms_by_class": {c: summary([r["ms"] for r in ok if r["class"] == c])
                                   for c in QUESTIONS},
            # Why the non-answers happened: the audit outcome (or the HTTP
            # code for a refusal that never reached the pipeline), the class
            # of question, and the message the user saw.
            "failures": _tally(
                (audit.get(r.get("request_id"), {}).get("status") or r.get("code")
                 or r.get("error") or "?",
                 r.get("class"), r.get("message"))
                for r in failed),
            "refused_overloaded": len(refused),
            "refused_ms": summary([r["ms"] for r in refused]),
        }
        if ph.get("window"):
            w = ph["window"]
            inside = [r for r in ok if w["start"] <= r["started"] <= w["end"]]
            outside = [r for r in ok if not (w["start"] <= r["started"] <= w["end"])]
            entry["during_publication_ms"] = summary([r["ms"] for r in inside])
            entry["outside_publication_ms"] = summary([r["ms"] for r in outside])
            entry["refresh_responses"] = statuses.get("refresh", 0)
        out.append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,4,8,16,32")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--no-publication", action="store_true")
    args = ap.parse_args()

    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    db = get_settings().db_name
    if db in PROTECTED:
        print(f"refusing to load-test {db!r}; use a disposable copy", file=sys.stderr)
        return 2
    with owner_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM sales")
        rows_in_db = cur.fetchone()["n"]

    users = provision()
    server = start_server(db)
    sampler = Sampler(db)
    sampler.start()
    rec = Recorder()
    phases = []
    try:
        for n in [int(x) for x in args.levels.split(",")]:
            phases.append(run_phase(f"c{n}", n, args.seconds, users, rec))
        if not args.no_publication:
            phases.append(run_phase("publish_in_week", 8, 60, users, rec,
                                    during=publish("in_week", 0)))
            phases.append(run_phase("publish_new_week", 8, 60, users, rec,
                                    during=publish("new_week", 7)))
            # Recovery: the same load once the publication -- including its
            # reclaim of the rows it replaced -- has finished.
            phases.append(run_phase("after_publication", 8, args.seconds, users, rec))
    finally:
        sampler.stop.set()
        server.terminate()
        server.wait(30)
    result = {
        "database": db, "sales_rows": rows_in_db, "workers": 2,
        "pool_max_per_worker": {"exec": 8, "scoped": 8, "auth": 4, "graph": 4},
        "users": {r: sum(u["role"] == r for u in users) for r in ROLE_WEIGHTS},
        "role_mix": ROLE_WEIGHTS, "class_mix": CLASS_WEIGHTS, "questions": QUESTIONS,
        "phases": report(rec.rows, phases),
        "db_connections_peak": sampler.peak if sampler.visible else None,
        "db_connections_active_peak": sampler.active_peak if sampler.visible else None,
        "model_cost": {
            "this_run": "offline planner; no model calls",
            "live_tokens_per_question": {"input": 4670, "output": 160,
                                         "source": "docs/EVALUATION.md, live runs 2026-09-25"},
            "per_answer": "input x PAC_LLM_INPUT_USD_PER_MTOK/1e6 + output x "
                          "PAC_LLM_OUTPUT_USD_PER_MTOK/1e6; a repaired plan costs two calls",
        },
    }
    remove(users)
    close_pools()
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
