"""Two controls that looked real and did nothing.

The metric registry is read at import, so a wheel without it cannot start --
and the wheel contained only .py files. `max_result_bytes` sat in the
configuration being read by no code at all: a row cap is not a size cap, and
5,000 rows of long organization names is a very different response from 5,000
rows of short codes.
"""

from __future__ import annotations

import json
import pathlib
import tomllib

import pytest

from app.analytics.plan import AnalyticalPlan
from app.analytics.render import render

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def test_the_registry_file_ships_with_the_package():
    """As an installed package would find it, not as a path next to the source."""
    from importlib.resources import files

    resource = files("app.analytics") / "metrics.yaml"
    assert resource.is_file(), "metrics.yaml is not a package resource"


def test_pyproject_declares_the_registry_as_package_data():
    """This declaration is what puts it in the wheel; without it the build
    emitted only .py files and an installed copy raised FileNotFoundError."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = config["tool"]["setuptools"].get("package-data", {})
    patterns = package_data.get("app.analytics", [])
    assert any(p.endswith(".yaml") or p == "metrics.yaml" for p in patterns), (
        f"app.analytics package-data does not cover the registry: {package_data}"
    )


def test_every_non_python_file_under_app_is_covered_by_package_data():
    """A new data file must not silently miss the wheel the way this one did."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = config["tool"]["setuptools"].get("package-data", {})
    covered = {pkg.replace(".", "/") for pkg in package_data}

    stray = [
        path.relative_to(ROOT)
        for path in (ROOT / "app").rglob("*")
        if path.is_file()
        and path.suffix not in (".py", ".pyc")
        and "__pycache__" not in path.parts
        and str(path.parent.relative_to(ROOT)) not in covered
    ]
    assert not stray, f"data files under app/ that no package-data pattern covers: {stray}"


# ---------------------------------------------------------------------------
# Result size
# ---------------------------------------------------------------------------

class FakeQuery:
    unit = "packs"
    metric_label = "paid pack units"
    window_label = "the last 3 months"
    comparison_label = None
    quality_checks: list[str] = []
    notes: list[str] = []


PLAN = AnalyticalPlan.model_validate({
    "metric": "paid_pack_units",
    "dimensions": ["account"],
    "time": {"kind": "named", "named": "r3m"},
})


def wide_rows(n: int):
    return [
        {"dim0_id": f"ORG-{i:06d}", "dim0_label": f"{'Very Long Hospital Name ' * 4}{i}",
         "value": float(i)}
        for i in range(n)
    ]


def rendered(rows, *, max_rows=5000, max_bytes=None):
    return render(rows, FakeQuery(), PLAN, scope_note="all", max_rows=max_rows,
                  max_bytes=max_bytes)


def test_a_large_result_is_trimmed_to_the_byte_limit():
    answer = rendered(wide_rows(2000), max_bytes=20_000)
    size = len(json.dumps(answer.table, default=str).encode())
    assert size <= 20_000, f"{size} bytes exceeds the limit"
    assert answer.truncated, "trimmed without saying so"
    assert answer.table, "trimmed away the entire answer"


def test_trimming_keeps_the_highest_ranked_rows():
    """Order carries the ranking, so the tail is what goes."""
    rows = wide_rows(2000)
    answer = rendered(rows, max_bytes=20_000)
    assert answer.table[0]["dim0_id"] == "ORG-000000"


def test_a_small_result_is_untouched_and_not_marked_truncated():
    answer = rendered(wide_rows(5), max_bytes=4_000_000)
    assert len(answer.table) == 5
    assert not answer.truncated


def test_no_limit_means_no_trimming():
    answer = rendered(wide_rows(300), max_bytes=None)
    assert len(answer.table) == 300


def test_the_row_cap_still_applies_independently():
    answer = rendered(wide_rows(50), max_rows=10, max_bytes=4_000_000)
    assert len(answer.table) == 10
    assert answer.truncated


def test_the_configured_default_is_actually_wired_up():
    """The setting existed and nothing read it."""
    import inspect

    from app.pipeline import Pipeline

    source = inspect.getsource(Pipeline.ask)
    assert "max_result_bytes" in source, (
        "max_result_bytes is configured but the pipeline does not pass it"
    )


@pytest.mark.parametrize("limit", [500, 5_000, 50_000])
def test_the_limit_is_respected_at_several_sizes(limit):
    answer = rendered(wide_rows(3000), max_bytes=limit)
    assert len(json.dumps(answer.table, default=str).encode()) <= limit
