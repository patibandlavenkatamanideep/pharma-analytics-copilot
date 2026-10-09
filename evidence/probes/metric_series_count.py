#!/usr/bin/env python3
"""How many metric series one serving process exports.

    python3 evidence/probes/metric_series_count.py --out result.json

Amazon Managed Service for Prometheus charges per sample ingested, and each
uvicorn worker exports every series every 15 s (app/telemetry.py), so the
series count is the input the staging estimate (infra/aws-staging/cost)
needs. This runs the real app in-process with the real instruments and the
freshness gauges configure() registers, on the database PAC_DB_NAME names (a
disposable one; two throwaway accounts are created and removed), and drives
a staging-like workload over HTTP:

* failed and successful sign-ins; the pages a browser loads;
* every question in evals/questions.yaml, holdout.yaml and holdout2.yaml,
  as an executive and as a user scoped like an existing scoped user, with a
  follow-up in each conversation;
* refused requests: unauthenticated, cross-origin, too large, unknown.

It then counts series as remote write stores them: one per counter or gauge
data point; per histogram data point one per bucket (+Inf included) plus
_sum and _count; and one target_info per process. The offline planner makes
no model call, so the model metrics (pac.llm.*) are counted separately as
the series a live provider would add.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import secrets
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    os.environ["PAC_COOKIE_SECURE"] = "false"
    os.environ["PAC_LLM_PROVIDER"] = "offline"
    os.environ.pop("PAC_OTEL_ENDPOINT", None)
    # This process only: every question is asked, rather than most refused by
    # the per-user rate limit (20 a minute), whose 429s are counted below.
    os.environ["PAC_USER_REQUESTS_PER_MINUTE"] = "1000"
    os.environ["PAC_USER_REQUESTS_PER_HOUR"] = "1000"

    from fastapi.testclient import TestClient
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

    from app import telemetry
    from app.auth.identity import set_credential
    from app.config import get_settings
    from app.data import freshness
    from app.db import owner_transaction

    get_settings.cache_clear()
    db = get_settings().db_name
    if db == "pharma_analytics":
        raise SystemExit("refusing the working database")

    questions = []
    for name in ("questions.yaml", "holdout.yaml", "holdout2.yaml"):
        doc = yaml.safe_load((ROOT / "evals" / name).read_text())
        items = doc if isinstance(doc, list) else next(v for v in doc.values() if isinstance(v, list))
        questions += [q["question"] for q in items if isinstance(q, dict) and q.get("question")]

    with owner_transaction() as cur:
        cur.execute("SELECT role, territory_name, region_name FROM users "
                    "WHERE role <> 'exec' AND territory_name IS NOT NULL ORDER BY user_id LIMIT 1")
        scoped = cur.fetchone()
    people = []
    for role, territory, region in (("exec", None, None),
                                    (scoped["role"], scoped["territory_name"], scoped["region_name"])):
        uid = f"series-probe-{role}-{secrets.token_hex(4)}"
        person = {"uid": uid, "email": f"{uid}@example.invalid", "password": secrets.token_urlsafe(18)}
        with owner_transaction() as cur:
            cur.execute("INSERT INTO users (user_id, email, full_name, role, territory_name, "
                        "region_name, can_view_wac) VALUES (%s, %s, 'Series probe', %s, %s, %s, 0)",
                        (uid, person["email"], role, territory, region))
        set_credential(uid, person["password"])
        people.append(person)

    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    telemetry.use(None, meters, freshness=telemetry.BoundedFreshness(freshness.read))
    statuses: collections.Counter = collections.Counter()
    result: dict = {"database": db, "questions_per_user": len(questions)}
    try:
        from app.api.main import app
        with TestClient(app) as client:
            def hit(method, path, **kw):
                r = client.request(method, path, **kw)
                statuses[f"{method} {path.split('/')[2] if path.startswith('/api/') else path} {r.status_code}"] += 1
                return r

            for path in ("/health", "/ready", "/api/auth/methods", "/api/me", "/api/conversations"):
                hit("GET", path)
            hit("POST", "/api/ask", json={"question": "total volume"})
            for person in people:
                client.cookies.clear()
                for _ in range(2):
                    hit("POST", "/api/login", json={"email": person["email"], "password": "wrong"})
                hit("POST", "/api/login", json={"email": person["email"], "password": person["password"]})
                hit("GET", "/api/me")
                for q in questions:
                    body = hit("POST", "/api/ask", json={"question": q}).json()
                    conv = body.get("conversation_id")
                    if conv:
                        hit("POST", "/api/ask", json={"question": "Break that down by quarter",
                                                      "conversation_id": conv})
                hit("GET", "/api/conversations")
                hit("GET", "/api/me/data")
                hit("GET", "/api/conversations/does-not-exist")
                hit("POST", "/api/ask", json={"question": "x"}, headers={"Origin": "https://elsewhere.example"})
                hit("POST", "/api/ask", content=b'{"question": "' + b"x" * 70_000 + b'"}',
                    headers={"Content-Type": "application/json"})
                hit("POST", "/api/logout")
            # What a rate-limited user sees, so its series are counted too: the
            # running pipeline reads the limit from this same settings object.
            get_settings().user_requests_per_minute = 1
            client.cookies.clear()
            hit("POST", "/api/login", json={"email": people[0]["email"], "password": people[0]["password"]})
            for _ in range(3):
                hit("POST", "/api/ask", json={"question": "What is our total volume this quarter?"})
            data = reader.get_metrics_data()
    finally:
        with owner_transaction() as cur:
            for person in people:
                cur.execute("DELETE FROM app_conv.turns WHERE conversation_id IN (SELECT conversation_id "
                            "FROM app_conv.conversations WHERE owner_user_id = %s)", (person["uid"],))
                cur.execute("DELETE FROM app_conv.conversations WHERE owner_user_id = %s", (person["uid"],))
                cur.execute("DELETE FROM app_auth.sessions WHERE user_id = %s", (person["uid"],))
                cur.execute("DELETE FROM app_auth.credentials WHERE user_id = %s", (person["uid"],))
                cur.execute("DELETE FROM app_auth.login_attempts WHERE email = %s", (person["email"],))
                cur.execute("DELETE FROM users WHERE user_id = %s", (person["uid"],))
        telemetry.reset()

    per_metric: dict[str, int] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                n = 0
                for p in m.data.data_points:
                    n += len(p.bucket_counts) + 2 if isinstance(p, HistogramDataPoint) else 1
                per_metric[m.name] = per_metric.get(m.name, 0) + n
    observed = sum(per_metric.values())
    llm = {k: v for k, v in per_metric.items() if k.startswith("pac.llm.")}
    result.update({
        "requests": dict(sorted(statuses.items())),
        "series_by_metric": dict(sorted(per_metric.items(), key=lambda kv: -kv[1])),
        "series_observed": observed,
        "target_info": 1,
        "series_per_process": observed + 1,
        "model_metrics_observed": llm,
        "note": "offline planner: no pac.llm.* series unless listed above",
    })
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: result[k] for k in ("questions_per_user", "series_observed",
                                             "series_per_process")}))
    print(json.dumps(result["series_by_metric"]))
    return 0 if observed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
