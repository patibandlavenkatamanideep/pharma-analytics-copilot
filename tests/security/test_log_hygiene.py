"""What the serving process writes to its logs is safe to collect centrally.

Review of 1 October 2026 ("Other work"): logs were Python logging's default
text, and span redaction never covered them. Several messages interpolate
exception text -- a planner failure can quote the user's question or an
entity, a database error the value it refused, a traceback all of it -- and
uvicorn's access log prints the full path, query string included: an OIDC
callback's code and state are in it, next to the client's address.

The first test reproduced it on the unmodified code
(evidence/runs/r3-logs-reproduced.json). It runs what a deployment runs: uvicorn's own logging configuration,
then the application's real startup, then log records shaped like the ones
the code emits, each carrying a marker that must not come out. Everything
the process writes to stdout and stderr is inspected.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from tests.security.conftest import AUTHTEST_DB

pytestmark = pytest.mark.security

ROOT = pathlib.Path(__file__).resolve().parents[2]

SCRIPT = r'''
import logging, logging.config
from uvicorn.config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)            # what `uvicorn app.api.main:app` installs
from fastapi.testclient import TestClient
from app.api.main import app

with TestClient(app):                                # the application's own startup
    for marker in ("MARKERsecret", "203.0.113.9", "Acme", "opaque-token-MARKER"):
        logging.getLogger("app.pipeline").warning(marker)
        logging.getLogger("thirdparty").warning("value %s", marker)
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d',
            "203.0.113.9", "GET", "/" + marker, "1.1", 404)
    logging.getLogger("app.pipeline").warning(
        "query failed (%s): %s", "sql", RuntimeError("value 'MARKER-cell-value' was refused"))
    try:
        raise RuntimeError("MARKER-in-the-traceback")
    except RuntimeError:
        logging.getLogger("app.api.main").exception(
            "unhandled pipeline failure (request_id=%s)", "f00dfeed00000001")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "203.0.113.9:5555", "GET",
        "/api/auth/oidc/callback?code=MARKER-code&state=MARKER-state", "1.1", 303)
'''


def run_process() -> str:
    env = {**os.environ, "PAC_DB_NAME": AUTHTEST_DB, "PYTHONPATH": str(ROOT),
           "PAC_LLM_PROVIDER": "offline"}
    env.pop("PAC_OTEL_ENDPOINT", None)
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout + proc.stderr


def test_the_process_logs_no_exception_text_query_strings_or_addresses(authtest_db):
    out = run_process()
    assert "MARKER" not in out, out[-3000:]
    assert "203.0.113.9" not in out
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    failure = next(r for r in lines if r["event"] == "request.failed")
    assert failure["error"]["type"] == "RuntimeError" and failure["level"] == "error"
    access = next(r for r in lines if r["event"] == "http.access" and r["status"] == 303)
    assert (access["path"], access["status"]) == ("/api/auth/oidc/callback", 303)


# -- correlation, through the real HTTP path -------------------------------------------

def collect():
    """What the process's log handler would write during the block."""
    import io
    import logging

    from app.logs import JsonFormatter
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    return buffer, handler


def test_every_response_carries_a_server_generated_request_id(client):
    first = client.get("/health")
    second = client.get("/health", headers={"X-Request-ID": "chosen-by-the-client"})
    ids = {first.headers["X-Request-ID"], second.headers["X-Request-ID"]}
    assert len(ids) == 2 and "chosen-by-the-client" not in ids


def test_a_log_line_names_the_http_request_its_turn_and_run(client, make_identity,
                                                            monkeypatch):
    """A warning logged inside the planning node -- a graph step, whichever
    thread runs it -- carries the response's X-Request-ID, and the audit
    request id and run id the response reports."""
    import logging

    from app.llm.planner import OfflinePlanner, PlannerError
    from tests.security.helpers import sign_in

    def refuse(self, question, context):
        raise PlannerError("could not plan 'MARKER-question-text'")

    monkeypatch.setattr(OfflinePlanner, "plan", refuse)
    sign_in(client, make_identity("exec", can_view_wac=1))
    buffer, handler = collect()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = client.post("/api/ask", json={"question": "What is our volume?"})
    finally:
        root.removeHandler(handler)
    body = response.json()
    lines = [json.loads(x) for x in buffer.getvalue().splitlines()]
    planner = next(r for r in lines if r["event"] == "planner.invalid")
    assert planner["http_id"] == response.headers["X-Request-ID"]
    assert (planner["request_id"], planner["run_id"]) == (body["request_id"], body["run_id"])
    assert "MARKER" not in buffer.getvalue()
