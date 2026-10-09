"""Structured diagnostic IDs only. Unknown/third-party messages and arguments
are never content-safe merely because they look like identifiers.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

REDACTED = "[redacted]"

# Exact code-owned templates map to stable events; no message is emitted.
EVENTS = {
    "startup.refused": "startup.refused",
    "web/dist not built; API only": "startup.web_missing",
    "serving dataset %s (%s), planner=%s": "startup.dataset_ready",
    "serving with %s (allowed only because PAC_ENVIRONMENT=local)": "startup.local_owner",
    "pool close failed": "db.pool_close_failed",
    "audit keys with no column, dropped: %s": "audit.schema_mismatch",
    "bedrock planner: model=%s endpoint=%s": "planner.configured",
    "starting with no published dataset (%s); /ready will report 503 until scripts/load_data.py has run": "startup.dataset_missing",
    "metric %s carried a label outside the allowlist; points dropped": "telemetry.labels_rejected",
    "planner unavailable: %s": "planner.unavailable",
    "planner failed: %s": "planner.invalid",
    "query failed (%s): %s": "query.failed",
    "failed to write audit row": "audit.write_failed",
    "replay withheld: its audit row could not be written": "audit.replay_withheld",
    "failed to commit turn for run %s": "turn.commit_failed",
    "generation changed mid-request (%s)": "dataset.changed",
    "could not prune checkpoint thread %s": "checkpoint.prune_failed",
    "turn graph exceeded %s transitions (run %s)": "graph.limit",
    "compiler produced SQL that failed validation: %s": "compiler.invalid",
    "grain violation for request %s: %s": "result.grain_invalid",
    "unhandled pipeline failure (request_id=%s)": "request.failed",
    "database unavailable on %s: %s": "db.unavailable",
    "not ready: database unavailable (%s)": "readiness.database_unavailable",
    "not ready: %s": "readiness.dataset_unavailable",
    "telemetry exporting to %s": "telemetry.configured",
    "telemetry shutdown failed": "telemetry.shutdown_failed",
    "telemetry not flushed within %.0f s; continuing without it": "telemetry.flush_timeout",
    "telemetry export failed (%s); %d spans dropped": "telemetry.spans_dropped",
    "metric export failed (%s); interval dropped": "telemetry.metrics_dropped",
    "telemetry exporter shutdown failed": "telemetry.exporter_shutdown_failed",
    "metric exporter shutdown failed": "telemetry.metric_shutdown_failed",
    "freshness could not be read (%s); not reported this time": "freshness.unavailable",
    "span attribute not recorded": "telemetry.attribute_dropped",
    "metric %s not recorded": "telemetry.metric_dropped",
    "telemetry not started (%s); ingesting without it": "ingest.telemetry_unavailable",
}
REASONS = {"database_unreachable", "security_boundary_broken", "owner_credential_present",
           "model_allowance_missing"}
ERROR_CODES = REASONS | {"browser_mismatch", "invalid_state", "expired_state"}
ERROR_TYPES = {"ValueError", "TypeError", "RuntimeError", "OperationalError",
               "PoolTimeout", "QueryCanceled", "CheckpointError", "PlannerError",
               "PlannerUnavailable", "PlannerBudgetExhausted", "TimeoutError"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
ROUTES = {"/", "/health", "/ready", "/api/ask", "/api/me", "/api/login", "/api/logout",
          "/api/auth/oidc/start", "/api/auth/oidc/callback", "/api/auth/oidc/cancel",
          "/api/conversations", "/api/feedback", "/api/auth/methods",
          "/api/runs/cancel", "/api/me/data"}


def route_template(path):
    path = str(path).split("?", 1)[0]
    if path in ROUTES:
        return path
    for pattern, template in [
        (r"/api/conversations/[^/]+", "/api/conversations/{conversation_id}"),
        (r"/api/runs/[^/]+", "/api/runs/{run_id}"),
        (r"/api/runs/[^/]+/cancel", "/api/runs/{run_id}/cancel"),
    ]:
        if re.fullmatch(pattern, path):
            return template
    return REDACTED


_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "pac_log_context", default={})

#: The loggers uvicorn configures with its own text handlers.
_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")


def bind(**ids: str | None) -> contextvars.Token:
    """Attach correlation ids to every record logged in this context."""
    return _context.set({**_context.get(), **{k: v for k, v in ids.items() if v}})


def unbind(token: contextvars.Token) -> None:
    _context.reset(token)


def current() -> dict[str, str]:
    return dict(_context.get())


def _driver_error(kind) -> bool:
    """psycopg's own error classes, each named after its SQLSTATE condition
    (InsufficientPrivilege, UniqueViolation, AdminShutdown, ...): a finite,
    code-defined set that says what kind of database failure it was and
    nothing about the data. A same-named class anywhere else does not count."""
    try:
        from psycopg import errors
    except ImportError:
        return False
    return (isinstance(kind, type) and issubclass(kind, errors.Error)
            and kind.__module__ in ("psycopg.errors", "psycopg"))


def _error(exc_info) -> dict[str, Any]:
    kind, value, _tb = exc_info
    name = getattr(kind, "__name__", "Exception")
    error = {"type": name if name in ERROR_TYPES or _driver_error(kind) else "Exception"}
    code = getattr(value, "code", None)
    if isinstance(code, str) and code in ERROR_CODES:
        error["code"] = code
    return error


class JsonFormatter(logging.Formatter):
    def __init__(self, release: str | None = None):
        super().__init__()
        self.release = release or os.environ.get("PAC_RELEASE", "dev")
        # Python warnings (a library's deprecation notice) otherwise print
        # straight to stderr as text. Routed through logging they become
        # records like any other. Here as well as in configure(), because
        # app/log_config.json builds this formatter before the app is
        # imported.
        logging.captureWarnings(True)

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name if record.name in {"app.pipeline", "app.api.main", "app.db",
                "app.llm.planner", "app.telemetry", "app.ingest", "uvicorn", "uvicorn.error",
                "uvicorn.access", "py.warnings"} else "external",
        }
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
            _client, method, path, _version, status = args
            out.update(event="http.access", method=method if isinstance(method, str) and method in METHODS else REDACTED,
                       path=route_template(path),
                       status=status if type(status) is int and 100 <= status <= 599 else None)
        else:
            msg = record.msg if isinstance(record.msg, str) else ""
            out["event"] = EVENTS.get(msg, "log.unclassified") if record.name.startswith("app.") else "log.external"
            if record.name == "uvicorn.error" and msg == "Application startup complete.":
                out["event"] = "server.started"
            if out["event"] == "startup.refused":
                reason = getattr(record, "reason", None)
                out["reason"] = reason if isinstance(reason, str) and reason in REASONS else "unknown"
        if record.exc_info and record.exc_info[0] is not None:
            out["error"] = _error(record.exc_info)
        out.update(_context.get())
        out["release"] = self.release
        return json.dumps(out, ensure_ascii=True, default=lambda _: REDACTED)


class _Handler(logging.StreamHandler):
    """Marks the handler this module installed, so configuring twice
    replaces it rather than doubling every line."""


def configure(settings=None) -> None:
    """Send every record -- the application's, uvicorn's and the libraries'
    -- through JsonFormatter on stderr. Idempotent. Leaves any other
    handler on the root logger alone (a test runner's, for instance)."""
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    if settings.log_format == "text":
        return
    logging.captureWarnings(True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _Handler):
            root.removeHandler(handler)
    # uvicorn started with app/log_config.json has installed it already.
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = _Handler(sys.stderr)
        handler.setFormatter(JsonFormatter(settings.release))
        root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    # uvicorn's own handlers print plain text, the access log with query
    # strings and addresses. Theirs go; records reach the root's instead.
    for name in _UVICORN:
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.propagate = True
