"""A log line carries what an allowlist names, and nothing else.

Review of 2 October 2026, finding 5. The previous round's formatter was
structured but not allowlisted. It kept the message template whatever it
was, so a message formatted before logging (an f-string with an error in it)
was kept whole. It kept any argument that looked like an identifier: an
opaque token, a one-word entity, a plain IP address. And it kept any
path-shaped access path. JSON structure alone is not sanitisation.

Each test carries a marker that must not come out. Written on the
parallel branch post-assessment/release-risks and ported here; all seven
leak on the reviewed code, 58b3d3e (evidence/runs/r5-port-reproduced.json).
No database: records go straight through the formatter, or through
telemetry.configure() with the freshness read stubbed.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys

from app.logs import JsonFormatter

#: Opaque, identifier-shaped, longer than any id the application issues.
TOKEN = "tok" + secrets.token_urlsafe(32)
ADDRESS = "203.0.113.9"


def emit(name: str, level: int, msg, *args, exc: BaseException | None = None) -> str:
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException:
            exc_info = sys.exc_info()
    record = logging.LogRecord(name, level, __file__, 1, msg, args or None, exc_info)
    text = JsonFormatter(release="r1").format(record)
    assert "\n" not in text
    json.loads(text)
    return text


def test_an_opaque_token_is_not_logged_whatever_the_message():
    for msg in ("could not prune checkpoint thread %s", "session %s refreshed"):
        assert TOKEN not in emit("app.pipeline", logging.WARNING, msg, TOKEN)


def test_a_one_word_entity_is_not_logged():
    for value in ("Zenovax", "zenovax"):
        for msg in ("planner failed: %s", "resolved %s"):
            out = emit("app.pipeline", logging.WARNING, msg, value)
            assert "zenovax" not in out.lower(), out


def test_a_message_formatted_before_logging_is_not_logged():
    error = RuntimeError("value 'MARKER-cell-value' was refused")
    out = emit("app.pipeline", logging.ERROR, f"query failed: {error}")
    assert "MARKER" not in out, out


def test_a_plain_address_is_not_logged():
    for msg, arg in (("telemetry exporting to %s", f"http://{ADDRESS}:4318"),
                     ("peer %s", ADDRESS)):
        assert ADDRESS not in emit("app.telemetry", logging.INFO, msg, arg)


def test_a_third_party_logger_keeps_nothing_it_was_given():
    pooled = emit("urllib3.connectionpool", logging.DEBUG, '%s://%s:%s "%s %s %s" %s %s',
                  "https", ADDRESS, 443, "POST", f"/model/{TOKEN}/invoke", "HTTP/1.1", 200, 512)
    found = emit("botocore.credentials", logging.INFO,
                 f"Found credentials in environment variables: {TOKEN}")
    for out in (pooled, found):
        assert TOKEN not in out and ADDRESS not in out, out


def test_an_access_path_is_not_logged():
    """uvicorn's access record: a path-shaped path with no query string."""
    out = emit("uvicorn.access", logging.INFO, '%s - "%s %s HTTP/%s" %d',
               "198.51.100.7:5555", "GET", f"/s/{TOKEN}", "1.1", 404)
    assert TOKEN not in out, out


def test_the_collector_address_is_not_logged_when_telemetry_starts(monkeypatch):
    """What telemetry.configure() really logs, with a collector configured
    by address. The freshness read is stubbed: no database here."""
    import io
    from types import SimpleNamespace

    from app import telemetry
    from app.data import freshness

    monkeypatch.setattr(freshness, "read", lambda: [])
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter(release="r1"))
    logger = logging.getLogger("app.telemetry")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    telemetry.reset()
    try:
        telemetry.configure(SimpleNamespace(otel_endpoint="http://127.0.0.1:9",
                                            otel_timeout_s=1, release="r1"))
    finally:
        telemetry.shutdown(timeout_s=5)
        logger.removeHandler(handler)
        logger.setLevel(previous)
    out = buffer.getvalue()
    assert out, "telemetry logged nothing at startup"
    assert "127.0.0.1" not in out, out
