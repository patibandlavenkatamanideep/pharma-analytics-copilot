"""The `--release-gate` pytest plugin.

`pytest tests/security -q` is documented as the release gate. On its own it
exits 0 when every test in it SKIPS -- which is exactly what happens with no
database loaded. A gate that reports success for a run that checked nothing is
worse than no gate, because it produces a green tick.

Under `--release-gate` a run has to assert what it actually proved: nothing was
skipped for a missing prerequisite, and at least `--min-tests` tests really
ran. Kept in its own module so it can be loaded on its own, including by the
tests that verify it.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--release-gate", action="store_true", default=False,
        help="fail if any test is skipped, or if fewer than --min-tests ran")
    parser.addoption(
        "--min-tests", type=int, default=1,
        help="minimum number of tests that must be collected under --release-gate")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--release-gate"):
        return
    minimum = config.getoption("--min-tests")
    if len(items) < minimum:
        raise pytest.UsageError(
            f"release gate: {len(items)} tests collected, expected at least "
            f"{minimum}. An empty or narrowed selection is not a pass."
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not item.config.getoption("--release-gate"):
        return
    if report.skipped and report.when == "setup":
        # A skip under the gate means a prerequisite was missing, so the gate
        # did not check what it claims to check.
        reason = getattr(report, "longrepr", None)
        report.outcome = "failed"
        report.longrepr = (
            "release gate: this test was skipped, so nothing was verified.\n"
            f"reason: {reason}"
        )
