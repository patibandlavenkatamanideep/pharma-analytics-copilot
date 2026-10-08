#!/usr/bin/env python3
"""Behind a load balancer, does one client's failed sign-ins lock out everyone?

    python3 evidence/probes/client_address_behind_proxy.py --out result.json

Sign-in is refused after 20 failures from one client address in 15 minutes
(app/auth/identity.py). uvicorn takes the client address from
X-Forwarded-For only when the connection comes from a trusted peer
(FORWARDED_ALLOW_IPS, default 127.0.0.1). Behind an ALB the peer is the load
balancer, so unless it is trusted every request appears to come from the
load balancer's own few addresses.

This runs the real server twice against the database PAC_DB_NAME names (a
disposable one; a throwaway account is created and removed). Each time, 20
failed sign-ins arrive "through the proxy" with 20 different X-Forwarded-For
addresses, then a different user signs in correctly from yet another one:

* FORWARDED_ALLOW_IPS empty -- the proxy is not trusted, as an ALB is by
  default;
* FORWARDED_ALLOW_IPS=127.0.0.1 -- the proxy is trusted, as
  infra/aws-staging configures the VPC range.

Exit 0 when the untrusted proxy shows the lockout and the trusted one does
not, i.e. when the configuration is what decides it.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(port: int, path: str, body: dict, forwarded: str) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-Forwarded-For": forwarded})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def run(trusted: str, user: dict) -> dict:
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PAC_LLM_PROVIDER": "offline",
           "PAC_COOKIE_SECURE": "false", "FORWARDED_ALLOW_IPS": trusted}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api.main:app", "--host",
                             "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        failures = [post(port, "/api/login",
                         {"email": f"nobody-{secrets.token_hex(4)}@example.invalid",
                          "password": "wrong"}, f"203.0.113.{i + 1}") for i in range(20)]
        victim = post(port, "/api/login", {"email": user["email"], "password": user["password"]},
                      "198.51.100.7")
        return {"FORWARDED_ALLOW_IPS": trusted or "(empty)", "failed_attempts": sorted(set(failures)),
                "other_user_correct_password": victim}
    finally:
        proc.terminate()
        proc.wait(20)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    from app.auth.identity import _hash_ip, set_credential
    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    db = get_settings().db_name
    if db == "pharma_analytics":
        raise SystemExit("refusing the working database")
    uid = f"proxy-probe-{secrets.token_hex(4)}"
    user = {"email": f"{uid}@example.invalid", "password": secrets.token_urlsafe(18)}
    with owner_transaction() as cur:
        cur.execute("INSERT INTO users (user_id, email, full_name, role, territory_name, "
                    "region_name, can_view_wac) VALUES (%s, %s, 'Proxy probe', 'exec', NULL, NULL, 0)",
                    (uid, user["email"]))
    set_credential(uid, user["password"])
    hashes = [_hash_ip(f"203.0.113.{i + 1}") for i in range(20)] + [_hash_ip(a) for a in
                                                                        ("127.0.0.1", "198.51.100.7")]
    result: dict = {"database": db}
    try:
        result["untrusted_proxy"] = run("", user)
        with owner_transaction() as cur:          # each run starts from no recent failures
            cur.execute("DELETE FROM app_auth.login_attempts WHERE ip_hash = ANY(%s)", (hashes,))
        result["trusted_proxy"] = run("127.0.0.1", user)
    finally:
        with owner_transaction() as cur:
            cur.execute("DELETE FROM app_auth.login_attempts WHERE ip_hash = ANY(%s) "
                        "OR email LIKE 'nobody-%%@example.invalid' OR email = %s",
                        (hashes, user["email"]))
            cur.execute("DELETE FROM app_auth.sessions WHERE user_id = %s", (uid,))
            cur.execute("DELETE FROM app_auth.credentials WHERE user_id = %s", (uid,))
            cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
        close_pools()
    result["lockout_without_trust"] = result["untrusted_proxy"]["other_user_correct_password"] == 429
    result["no_lockout_with_trust"] = result["trusted_proxy"]["other_user_correct_password"] == 200
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result))
    return 0 if result["lockout_without_trust"] and result["no_lockout_with_trust"] else 1


if __name__ == "__main__":
    sys.exit(main())
