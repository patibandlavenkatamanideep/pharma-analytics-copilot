"""What the serving process writes to its logs is safe to collect centrally.

Review of 1 October 2026 ("Other work"): logs were Python logging's default
text, and span redaction never covered them. Several messages interpolate
exception text -- a planner failure can quote the user's question or an
entity, a database error the value it refused, a traceback all of it -- and
uvicorn's access log prints the full path, query string included: an OIDC
callback's code and state are in it, next to the client's address.

The test runs what a deployment runs: uvicorn's own logging configuration,
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

LOGS = pytest.mark.xfail(strict=True, reason="logs carry exception text, query strings and "
                                             "client addresses")

SCRIPT = r'''
import logging, logging.config
from uvicorn.config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)            # what `uvicorn app.api.main:app` installs
from fastapi.testclient import TestClient
from app.api.main import app

with TestClient(app):                                # the application's own startup
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


@LOGS
def test_the_process_logs_no_exception_text_query_strings_or_addresses(authtest_db):
    out = run_process()
    assert "MARKER" not in out, out[-3000:]
    assert "203.0.113.9" not in out
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    failure = next(r for r in lines if r["event"].startswith("unhandled pipeline failure"))
    assert failure["error"]["type"] == "RuntimeError" and failure["level"] == "error"
    access = next(r for r in lines if r["logger"] == "uvicorn.access")
    assert (access["path"], access["status"]) == ("/api/auth/oidc/callback", 303)
