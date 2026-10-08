"""The log formatter: templates not text, safe arguments only, an exception's
type never its message, correlation ids, and access lines without query
strings or addresses (app/logs.py)."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from app import logs
from app.logs import REDACTED, JsonFormatter


def record(msg, *args, name="app.pipeline", level=logging.WARNING, exc=None):
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException:
            exc_info = sys.exc_info()
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def line(rec) -> dict:
    text = JsonFormatter(release="r1").format(rec)
    assert "\n" not in text
    return json.loads(text)


def test_the_template_is_kept_and_the_interpolation_never_happens():
    out = line(record("query failed (%s): %s", "sql",
                      RuntimeError("value 'alice@example.com' refused")))
    assert out["event"] == "query.failed"
    assert "args" not in out
    assert "alice" not in json.dumps(out)


def test_identifier_shaped_arguments_are_not_safe():
    out = line(record("%s %s %s %s %s %s", "us.anthropic.claude-opus-4-5-20251101-v1:0",
                      "/api/runs/r_abc", 42, 1.5, None,
                      "What were Northeast sales for Zenovax?"))
    assert "args" not in out
    assert out["event"] == "log.unclassified"


def test_an_email_or_address_is_not_an_identifier():
    out = line(record("%s %s", "alice@example.com", "203.0.113.9:5555 extra"))
    assert "args" not in out


def test_an_exception_contributes_its_type_and_place_not_its_message():
    out = line(record("unhandled pipeline failure (request_id=%s)", "f00dfeed00000001",
                      level=logging.ERROR, exc=ValueError("secret cell 'MARKER'")))
    assert out["error"]["type"] == "ValueError"
    assert "MARKER" not in json.dumps(out) and "Traceback" not in json.dumps(out)


def test_a_stable_error_code_is_kept():
    class Coded(Exception):
        code = "browser_mismatch"
    assert line(record("refused", exc=Coded("detail"))) ["error"]["code"] == "browser_mismatch"


def test_a_mapping_argument_is_sanitised_too():
    out = line(record("%(route)s %(question)s", {"route": "/api/ask",
                                                 "question": "Top accounts in Ohio?"}))
    assert "args" not in out


def test_an_access_line_has_no_query_string_and_no_client_address():
    out = line(record('%s - "%s %s HTTP/%s" %d', "203.0.113.9:5555", "GET",
                      "/api/auth/oidc/callback?code=MARKER&state=MARKER", "1.1", 303,
                      name="uvicorn.access", level=logging.INFO))
    assert out["event"] == "http.access"
    assert (out["method"], out["path"], out["status"]) == ("GET", "/api/auth/oidc/callback", 303)
    assert "MARKER" not in json.dumps(out) and "203.0.113.9" not in json.dumps(out)


def test_a_path_that_is_not_path_shaped_is_redacted():
    out = line(record('%s - "%s %s HTTP/%s" %d', "x", "GET", "/a b/<script>", "1.1", 404,
                      name="uvicorn.access", level=logging.INFO))
    assert out["path"] == REDACTED


def test_bound_ids_are_on_every_line_and_go_when_unbound():
    token = logs.bind(http_id="h1", request_id="q1", run_id="r_1")
    try:
        out = line(record("x"))
        assert (out["http_id"], out["request_id"], out["run_id"]) == ("h1", "q1", "r_1")
    finally:
        logs.unbind(token)
    assert "http_id" not in line(record("x"))
    assert line(record("x"))["release"] == "r1"


def test_configuring_twice_does_not_double_lines(monkeypatch):
    from types import SimpleNamespace
    root = logging.getLogger()
    before = list(root.handlers)
    settings = SimpleNamespace(log_format="json", log_level="INFO", release="r1")
    try:
        logs.configure(settings)
        logs.configure(settings)
        ours = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
        assert len(ours) == 1
        assert not logging.getLogger("uvicorn.access").handlers
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_a_python_warning_becomes_a_record_not_a_stray_line():
    """In a fresh process: pytest intercepts warnings itself, so this cannot
    be observed in-process."""
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    script = ("import logging, warnings\n"
              "from app.logs import JsonFormatter\n"
              "h = logging.StreamHandler(); h.setFormatter(JsonFormatter('r1'))\n"
              "logging.getLogger().addHandler(h)\n"
              "warnings.warn('something is deprecated', DeprecationWarning)\n")
    proc = subprocess.run([sys.executable, "-W", "always", "-c", script], cwd=root,
                          capture_output=True, text=True, timeout=60)
    lines = [x for x in proc.stderr.splitlines() if x.strip()]
    assert lines and all(x.startswith("{") for x in lines), proc.stderr
    out = json.loads(lines[-1])
    assert out["logger"] == "py.warnings" and out["level"] == "warning"


@pytest.mark.xfail(strict=True, reason="reproduction: a database error's kind is logged as Exception")
def test_a_database_error_keeps_its_sqlstate_class_name():
    """psycopg names each error class after its SQLSTATE condition: a finite,
    code-defined set. Reduced to "Exception", a refused audit insert (the
    drill's AuditLoss scenario) cannot be told from a lost connection or a
    constraint violation in the log."""
    import psycopg.errors

    out = line(record("failed to write audit row", level=logging.ERROR,
                      exc=psycopg.errors.InsufficientPrivilege("permission denied for MARKER")))
    assert out["error"]["type"] == "InsufficientPrivilege"
    assert "MARKER" not in json.dumps(out)


def test_a_lookalike_class_outside_the_driver_stays_generic():
    class InsufficientPrivilege(Exception):
        pass
    out = line(record("failed to write audit row", level=logging.ERROR,
                      exc=InsufficientPrivilege("x")))
    assert out["error"]["type"] == "Exception"
