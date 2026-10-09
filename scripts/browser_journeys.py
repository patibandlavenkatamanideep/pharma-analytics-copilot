#!/usr/bin/env python3
"""Run the real-browser journeys against a real local server.

Provisions disposable identities -- and two clinics that share a name, for
the clarification journey -- in the DISPOSABLE authorization database, never
the working one; starts the application against it; runs Playwright; and
removes everything it created, whatever the outcome.

    python3 scripts/browser_journeys.py          # needs web/dist built and Chromium

Credentials exist only in this process's environment and the child
processes it starts; nothing is written to disk.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = os.environ.get("PAC_AUTHTEST_DB", "pharma_analytics_authtest")
PORT = 8010
TWIN = "Journey Twin Clinic"


def main() -> int:
    os.environ["PAC_DB_NAME"] = DB
    from app.auth.identity import set_credential
    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    get_settings.cache_clear()
    suffix = secrets.token_hex(3)
    users = {}
    orgs = [f"SA-JT1-{suffix.upper()}", f"SA-JT2-{suffix.upper()}"]
    with owner_transaction() as cur:
        cur.execute("SELECT territory_name, region_name FROM zip_territory ORDER BY 1 LIMIT 1")
        place = cur.fetchone()
        for key, role, wac, territory, region in (
                ("EXEC", "exec", 1, None, None), ("OTHER", "exec", 1, None, None),
                ("RAM", "ram", 0, place["territory_name"], place["region_name"])):
            user_id = f"pacjourney-{key.lower()}-{suffix}"
            email = f"{user_id}@test.invalid"
            cur.execute(
                "INSERT INTO users (user_id, email, full_name, role, territory_name, "
                "region_name, can_view_wac) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (user_id, email, f"Journey {key.title()}", role, territory, region, wac))
            users[key] = (user_id, email, secrets.token_urlsafe(18))
        for org_id, state, zip_code in ((orgs[0], "TX", "75201"), (orgs[1], "OR", "97201")):
            cur.execute("INSERT INTO organizations (org_id, org_name, org_type, org_status, "
                        "state, zip) VALUES (%s, %s, 'Facility', 'Active', %s, %s)",
                        (org_id, TWIN, state, zip_code))
    for user_id, _, password in users.values():
        set_credential(user_id, password)

    env = {**os.environ, "PAC_DB_NAME": DB, "PAC_COOKIE_SECURE": "false",
           "PAC_LLM_PROVIDER": "offline", "PAC_ENVIRONMENT": "local",
           "PAC_USER_REQUESTS_PER_MINUTE": "1000", "PAC_USER_CONCURRENT_RUNS": "10"}
    # The production log configuration (sanitised JSON lines: no exception
    # text, query strings or client addresses), written beside the test
    # results (Playwright empties its own test-results/ when it starts) so a
    # failure keeps the server's side of it. An unread pipe here would also
    # stop the server once it filled.
    logs = ROOT / "web" / "e2e-logs"
    logs.mkdir(parents=True, exist_ok=True)
    server_log = (logs / "server.log").open("w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api.main:app", "--port", str(PORT),
         "--log-config", "app/log_config.json"],
        cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("server did not start", file=sys.stderr)
            return 2
        run_env = {**env, "PAC_E2E_URL": f"http://127.0.0.1:{PORT}", "PAC_E2E_TWIN": TWIN}
        for key, (_, email, password) in users.items():
            prefix = "PAC_E2E" if key == "EXEC" else f"PAC_E2E_{key}"
            run_env[f"{prefix}_EMAIL"] = email
            run_env[f"{prefix}_PASSWORD"] = password
        result = subprocess.run(["npx", "playwright", "test", *sys.argv[1:]],
                                cwd=ROOT / "web", env=run_env)
        return result.returncode
    finally:
        server.terminate()
        server.wait(timeout=10)
        server_log.close()
        with owner_transaction() as cur:
            ids = [u[0] for u in users.values()]
            cur.execute("DELETE FROM app_conv.turns WHERE conversation_id IN "
                        "(SELECT conversation_id FROM app_conv.conversations "
                        " WHERE owner_user_id = ANY(%s))", (ids,))
            cur.execute("DELETE FROM app_conv.conversations WHERE owner_user_id = ANY(%s)", (ids,))
            for table in ("app_auth.sessions", "app_auth.credentials", "app_auth.identities"):
                cur.execute(f"DELETE FROM {table} WHERE user_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM users WHERE user_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM organizations WHERE org_id = ANY(%s)", (orgs,))
        close_pools()


if __name__ == "__main__":
    sys.exit(main())
