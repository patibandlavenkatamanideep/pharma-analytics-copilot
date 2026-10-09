"""Every log call the code makes is a registered event.

app/logs.py maps exact code-owned templates on app.* loggers to event IDs
and drops every argument. A call whose template is not in EVENTS -- or
whose logger is not under app. -- is still safe, but it logs only
"log.unclassified" or "log.external": the event loses its meaning. Three
telemetry debug messages and the ingestion job's telemetry warning had
drifted that way. This check fails when a log call is added or reworded
without registering it.
"""

from __future__ import annotations

import ast
import pathlib

from app.logs import EVENTS

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}


def log_calls():
    """(file, line, logger name, literal template or None) for every call on
    a module-level logger in app/ and scripts/."""
    for path in sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]):
        tree = ast.parse(path.read_text())
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        loggers = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                    and getattr(node.value.func, "attr", "") == "getLogger" and node.value.args:
                arg = node.value.args[0]
                name = module if isinstance(arg, ast.Name) and arg.id == "__name__" else \
                    arg.value if isinstance(arg, ast.Constant) else None
                for target in node.targets:
                    if isinstance(target, ast.Name) and name:
                        loggers[target.id] = name
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in loggers \
                    and node.func.attr in LEVELS:
                first = node.args[0] if node.args else None
                template = first.value if isinstance(first, ast.Constant) \
                    and isinstance(first.value, str) else None
                yield path.relative_to(ROOT), node.lineno, loggers[node.func.value.id], template


def test_every_log_call_is_a_registered_event_on_an_application_logger():
    calls = list(log_calls())
    assert len(calls) > 30, "the scan found almost no log calls; is it still reading the code?"
    problems = [f"{path}:{line}: " + ("the message is not a literal template" if template is None
                                      else f"logger {logger!r} is not under app." if not logger.startswith("app.")
                                      else f"{template!r} is not in app.logs.EVENTS")
                for path, line, logger, template in calls
                if template is None or not logger.startswith("app.") or template not in EVENTS]
    assert not problems, "\n".join(problems)
