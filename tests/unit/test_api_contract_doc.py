"""docs/API.md describes exactly the routes that exist.

An API document that lags the code is worse than none: a client written
against it fails in ways the document says cannot happen. This compares the
document's endpoint headings with the application's routes in both
directions, and its version with the one the API announces.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DOC = (ROOT / "docs" / "API.md").read_text()


def documented() -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2))
            for m in re.finditer(r"^### (GET|POST|PUT|PATCH|DELETE) (\S+)$", DOC, re.M)}


def served() -> set[tuple[str, str]]:
    from app.api.main import app
    out = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if route.path.startswith(("/api/", "/health", "/ready")):
            out |= {(m, route.path) for m in methods - {"HEAD"}}
    return out


def test_every_route_is_documented():
    missing = served() - documented()
    assert not missing, f"routes with no section in docs/API.md: {sorted(missing)}"


def test_every_documented_route_exists():
    stale = documented() - served()
    assert not stale, f"docs/API.md describes routes that do not exist: {sorted(stale)}"


def test_the_document_states_the_version_the_api_announces():
    from app.api.main import API_VERSION
    assert f"Version **{API_VERSION}**" in DOC
    assert f"X-API-Version: {API_VERSION}" in DOC
