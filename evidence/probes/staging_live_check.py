#!/usr/bin/env python3
"""The deployed staging site, checked over HTTPS from outside, without printing a secret.

    AWS_PROFILE=<sso profile> python3 evidence/probes/staging_live_check.py \\
        --url https://staging.example.org --test-users-secret <arn>

What a reviewer's browser goes through, as plain HTTPS requests against the
public hostname (verified TLS, the load balancer, the serving task):

* HTTP redirects to HTTPS; /health names the release; /ready is ready;
* the sign-in page carries Strict-Transport-Security, X-Content-Type-Options
  nosniff and X-Frame-Options DENY (the load balancer adds them);
* an unknown account is refused, and a cross-site POST is refused before
  any route runs;
* U001 (an executive) signs in, sees the full dataset, gets an answer to a
  revenue question (sec-03), and after sign-out is no longer signed in;
* U003 (a regional director, no pricing) asking for revenue gets no
  pricing (sec-01, judged as scripts/run_evals.py judges volume_alternative:
  refused with an alternative, or answered in volume with the restriction
  disclosed, no currency anywhere a reader looks, and SQL that does not read
  the price column), and a volume answer carries its scope.

Test-user passwords come from the test-users secret (seed-secrets.sh), read
in this process and never printed. Prints one JSON document of check names,
outcomes and non-secret facts (release, dataset mode and rows, statuses).
Exits 1 if any check fails. Read-only apart from sign-in, two conversations
and the per-address sign-in counters.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

USERS = {"U001": "sarah.chen@novapharma.com", "U003": "jennifer.walsh@novapharma.com"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Site:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def call(self, method: str, path: str, body: dict | None = None, origin: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Origin", origin or self.url)
        try:
            with self.opener.open(req, timeout=60) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {}


def no_pricing(ans: dict) -> bool:
    """scripts/run_evals.py's volume_alternative rule, on the API's JSON."""
    if ans.get("status") == "denied":
        return bool(ans.get("alternative"))
    answer = ans.get("answer") or {}
    if ans.get("status") != "answered" or not answer:
        return False
    notes = " ".join(answer.get("notes") or []).lower()
    readable = [answer.get("headline"), answer.get("scope_note"), answer.get("period_note")]
    readable += list(answer.get("notes") or []) + list(answer.get("warnings") or [])
    for row in answer.get("rows") or []:
        readable += [str(v) for v in (row.values() if isinstance(row, dict) else row)]
    body = " ".join(str(p) for p in readable if p)
    columns = answer.get("columns") or []
    last = columns[-1] if columns else ""
    last = (last.get("label") or last.get("name") or "") if isinstance(last, dict) else str(last)
    return (("pricing" in notes or "restricted" in notes) and "$" not in body
            and "usd" not in last.lower() and "wac" not in str(ans.get("sql") or "").lower())


def passwords(secret: str) -> dict:
    out = subprocess.run(["aws", "secretsmanager", "get-secret-value", "--secret-id", secret,
                          "--query", "SecretString", "--output", "text"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--test-users-secret", required=True)
    args = ap.parse_args()
    checks, facts = {}, {}

    def check(name: str, ok: bool) -> None:
        checks[name] = "pass" if ok else "FAIL"

    plain = urllib.request.build_opener(NoRedirect)
    try:
        plain.open(args.url.replace("https://", "http://") + "/health", timeout=30)
        check("http_redirects_to_https", False)
    except urllib.error.HTTPError as e:
        check("http_redirects_to_https", e.code in (301, 308) and e.headers.get("Location", "").startswith("https://"))

    site = Site(args.url)
    status, health = site.call("GET", "/health")
    facts["release"] = health.get("release")
    check("health", status == 200 and health.get("status") == "ok")
    status, ready = site.call("GET", "/ready")
    check("ready", status == 200)
    with urllib.request.urlopen(args.url.rstrip("/") + "/", timeout=30) as r:
        h = r.headers
    facts["security_headers"] = {k: h.get(k) for k in ("Strict-Transport-Security", "X-Content-Type-Options",
                                                        "X-Frame-Options")}
    check("security_headers", (h.get("Strict-Transport-Security") or "").startswith("max-age=")
          and h.get("X-Content-Type-Options") == "nosniff" and h.get("X-Frame-Options") == "DENY")

    status, _ = site.call("POST", "/api/login", {"email": f"nobody-{secrets.token_hex(4)}@example.invalid",
                                                 "password": secrets.token_urlsafe(16)})
    check("unknown_account_refused", status == 401)
    status, _ = site.call("POST", "/api/login", {"email": "x@example.invalid", "password": "x"},
                          origin="https://other-site.example")
    check("cross_site_post_refused", status == 403)

    pw = passwords(args.test_users_secret)

    exec_site = Site(args.url)
    status, _ = exec_site.call("POST", "/api/login", {"email": USERS["U001"], "password": pw["U001"]})
    check("exec_signs_in", status == 200)
    status, me = exec_site.call("GET", "/api/me")
    user = (me.get("user") or {}) if status == 200 else {}
    dataset = (me.get("dataset") or {}) if status == 200 else {}
    facts.update(exec_role=user.get("role"), dataset_mode=dataset.get("mode"), dataset_rows=dataset.get("rows"))
    check("exec_sees_full_dataset", user.get("role") == "exec" and dataset.get("mode") == "full"
          and (dataset.get("rows") or {}).get("sales") == 2_000_000)
    status, ans = exec_site.call("POST", "/api/ask", {"question": "What is our total revenue this quarter?"})
    facts["exec_revenue_status"] = ans.get("status")
    check("exec_revenue_answered", status == 200 and ans.get("status") == "answered")
    exec_site.call("POST", "/api/logout")
    status, _ = exec_site.call("GET", "/api/me")
    check("signed_out_after_logout", status == 401)

    director = Site(args.url)
    status, _ = director.call("POST", "/api/login", {"email": USERS["U003"], "password": pw["U003"]})
    check("director_signs_in", status == 200)
    status, ans = director.call("POST", "/api/ask", {"question": "What is our revenue this quarter?",
                                                     "include_sql": True})
    facts["director_revenue_status"] = ans.get("status")
    check("director_revenue_no_pricing", no_pricing(ans))
    status, ans = director.call("POST", "/api/ask", {"question": "Compare all territories by volume"})
    answer = ans.get("answer") or {}
    facts["director_volume_status"] = ans.get("status")
    check("director_volume_scoped", ans.get("status") == "answered" and bool(answer.get("scope_note")))
    director.call("POST", "/api/logout")
    del pw

    failed = [k for k, v in checks.items() if v != "pass"]
    print(json.dumps({"checks": checks, "facts": facts, "failed": failed}, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
