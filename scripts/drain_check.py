#!/usr/bin/env python3
"""Graceful shutdown, measured: SIGTERM while answers are in flight.

Starts the server the way the image does (uvicorn, 2 workers,
--timeout-graceful-shutdown 65), sends several slow questions, then SIGTERM
half a second later. Every question already in flight must be answered, and
the process must exit once the answers are out. Prints one JSON document.

Whether a connection made just after the signal is still served is a race
between the signal reaching each worker and that worker's next accept --
observed both ways -- so it is reported, not asserted. Routing traffic away
before stopping is the orchestrator's job (readiness), not the server's.

Run against a DISPOSABLE full-size copy (slow questions need real data):

    PAC_DB_NAME=pharma_analytics_ingestscale python3 scripts/drain_check.py
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import signal
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8030
#: Few enough that each slow question finishes inside the 5 s statement
#: timeout; six at once pushed them over it, which tests the timeout, not
#: the drain.
IN_FLIGHT = 4
SLOW = "paid pack units by facility and month, all time"


def main() -> int:
    import httpx2

    from app.auth.identity import set_credential
    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    db = get_settings().db_name
    if db == "pharma_analytics":
        print("refusing the working database; use a disposable copy", file=sys.stderr)
        return 2

    user_id, password = f"pacdrain-{secrets.token_hex(4)}", secrets.token_urlsafe(18)
    with owner_transaction() as cur:
        cur.execute("INSERT INTO users (user_id, email, full_name, role, can_view_wac) "
                    "VALUES (%s, %s, 'Drain check', 'exec', 1)",
                    (user_id, f"{user_id}@drain.invalid"))
    set_credential(user_id, password)

    env = {**os.environ, "PAC_DB_NAME": db, "PAC_COOKIE_SECURE": "false",
           "PAC_LLM_PROVIDER": "offline", "PAC_ENVIRONMENT": "local",
           "PAC_USER_CONCURRENT_RUNS": "100", "PYTHONPATH": str(ROOT)}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--workers", "2", "--timeout-graceful-shutdown", "65",
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{PORT}"
    try:
        for _ in range(120):
            try:
                if httpx2.get(f"{base}/ready", timeout=2).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        results: list[dict] = []
        lock = threading.Lock()

        def ask(i):
            client = httpx2.Client(base_url=base, timeout=120)
            client.post("/api/login", json={"email": f"{user_id}@drain.invalid",
                                             "password": password})
            ready.wait()
            t0 = time.perf_counter()
            try:
                r = client.post("/api/ask", json={"question": SLOW},
                                headers={"Idempotency-Key": f"drain-{i}-{secrets.token_hex(4)}"})
                out = {"http": r.status_code, "status": r.json().get("status")}
            except Exception as exc:
                out = {"http": None, "status": type(exc).__name__}
            out["seconds"] = round(time.perf_counter() - t0, 2)
            with lock:
                results.append(out)

        ready = threading.Event()
        threads = [threading.Thread(target=ask, args=(i,)) for i in range(IN_FLIGHT)]
        for t in threads:
            t.start()
        time.sleep(1.0)              # every client signed in
        ready.set()
        time.sleep(0.5)              # every question in flight
        signalled = time.perf_counter()
        server.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        try:
            late = httpx2.get(f"{base}/health", timeout=2).status_code
        except Exception as exc:
            late = type(exc).__name__
        for t in threads:
            t.join(120)
        server.wait(90)
        exited = round(time.perf_counter() - signalled, 2)
    finally:
        if server.poll() is None:
            server.kill()
        with owner_transaction() as cur:
            cur.execute("DELETE FROM app_conv.conversations WHERE owner_user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_conv.runs WHERE owner_user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_auth.sessions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_auth.credentials WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_meta.query_audit WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        close_pools()

    answered = sum(r["status"] == "answered" for r in results)
    complete = sum(r["http"] == 200 and r["status"] is not None for r in results)
    report = {"in_flight": len(results), "complete_responses": complete,
              "answered": answered, "results": results,
              "request_after_signal_observed": late, "exit_seconds_after_signal": exited,
              "exit_code": server.returncode}
    print(json.dumps(report))
    ok = (complete == answered == len(results) == IN_FLIGHT
          and server.returncode == 0 and exited < 65)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
