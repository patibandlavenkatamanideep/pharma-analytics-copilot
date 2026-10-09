"""The `--release-gate` pytest plugin.

`pytest tests/security -q` is documented as the release gate. On its own it
exits 0 when every test in it SKIPS -- which is exactly what happens with no
database loaded. A gate that reports success for a run that checked nothing is
worse than no gate, because it produces a green tick.

Under `--release-gate` a run has to assert what it actually proved. The first
version of this plugin only converted skips reported during **setup**, on the
assumption that a skip always comes from an unavailable fixture. It does not:
a test that calls ``pytest.skip()`` in its own body skips at CALL phase, and a
module that calls ``pytest.skip(allow_module_level=True)`` never produces a
test item at all. Both walked straight through the gate and exited 0.

So the rule is now about outcomes rather than phases:

* **Any** skip fails the run -- setup, call or teardown -- because under the
  gate a skip means a requirement was not checked.
* A **collection** error or skip fails the run, so a module that opts out
  wholesale cannot disappear silently.
* An **xfail or xpass** fails the run unless ``--allow-xfail`` is passed. A
  known-failing security test is a known-failing security requirement; it
  belongs outside the mandatory gate, not inside it wearing a marker.
* A session in which **no test actually passed** fails, even if nothing
  failed either. Zero of zero is not a green gate.
* ``--min-tests`` remains, but only as a secondary guard against a narrowed
  selection. It is not the definition of the requirement: a hundred collected
  tests that all skip still fail.

Kept in its own module so it can be loaded on its own, including by the
subprocess tests that verify it.
"""

from __future__ import annotations

import pytest

#: Reasons the gate rejected the run. Collected across hooks and reported
#: once at the end, so a failing gate explains itself rather than leaving the
#: reader to infer it from a wall of individual failures.
_VIOLATIONS_KEY = pytest.StashKey[list]() if hasattr(pytest, "StashKey") else None


def pytest_addoption(parser):
    group = parser.getgroup("release gate")
    group.addoption(
        "--release-gate", action="store_true", default=False,
        help="fail the run on any skip, collection error, unapproved xfail, "
             "empty selection, or a session in which nothing passed")
    group.addoption(
        "--min-tests", type=int, default=1,
        help="secondary guard: minimum tests that must be COLLECTED under "
             "--release-gate. Not the requirement itself -- collected tests "
             "that skip still fail the gate")
    group.addoption(
        "--allow-xfail", action="store_true", default=False,
        help="permit xfail/xpass under --release-gate. Off by default: a "
             "known-failing security test is a known-failing security "
             "requirement")


def _enabled(config) -> bool:
    return bool(config.getoption("--release-gate", default=False))


def _violations(config) -> list[str]:
    store = getattr(config, "_release_gate_violations", None)
    if store is None:
        store = []
        config._release_gate_violations = store
    return store


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # trylast so this sees the list AFTER -k/-m deselection. Running earlier
    # counted items that were about to be filtered out, so a narrowed
    # selection satisfied --min-tests and then ran almost nothing.
    if not _enabled(config):
        return
    minimum = config.getoption("--min-tests")
    if len(items) < minimum:
        raise pytest.UsageError(
            f"release gate: {len(items)} tests collected, expected at least "
            f"{minimum}. An empty or narrowed selection is not a pass."
        )


def pytest_collectreport(report):
    """A module that skips or fails at import must not vanish quietly.

    ``pytest.skip(allow_module_level=True)`` produces no test items, so
    nothing reaches the per-test hook and ``--min-tests`` sees a smaller
    collection rather than a violation.
    """
    config = getattr(report, "config", None)
    if config is None:                      # older pytest: handled in sessionfinish
        return
    if not _enabled(config):
        return
    if report.outcome in ("skipped", "failed"):
        _violations(config).append(
            f"collection of {report.nodeid or '<session>'} was {report.outcome}: "
            f"a module that does not collect cannot have been verified"
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not _enabled(item.config):
        return

    # xfail and xpass both arrive with wasxfail set. Under the gate they are
    # failures unless explicitly permitted: an expected failure in the
    # security suite is an unmet security requirement with a label on it.
    if hasattr(report, "wasxfail") and not item.config.getoption("--allow-xfail"):
        report.outcome = "failed"
        report.longrepr = (
            "release gate: expected-failure markers are not permitted here.\n"
            "A known-failing security requirement belongs outside the "
            "mandatory gate, not inside it as an xfail.\n"
            f"marker reason: {report.wasxfail or '(none given)'}"
        )
        _violations(item.config).append(f"{item.nodeid}: xfail/xpass under the gate")
        return

    if hasattr(report, "wasxfail"):
        # Permitted by --allow-xfail. Return before the skip rule below: an
        # xfail report also carries skipped=True, so falling through would
        # convert the very thing that was just allowed into a failure.
        return

    if report.when == "call" and report.passed:
        item.config._release_gate_passed = getattr(
            item.config, "_release_gate_passed", 0) + 1

    # ANY skip, in ANY phase. The previous version checked only setup, so a
    # pytest.skip() inside a test body passed the gate.
    if report.skipped:
        reason = getattr(report, "longrepr", None)
        report.outcome = "failed"
        report.longrepr = (
            f"release gate: this test was skipped during {report.when}, so "
            "nothing was verified.\n"
            f"reason: {reason}"
        )
        _violations(item.config).append(
            f"{item.nodeid}: skipped during {report.when}")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def pytest_sessionfinish(session, exitstatus):
    """Fail a session that proved nothing, and explain every violation once."""
    config = session.config
    if not _enabled(config):
        return

    violations = _violations(config)

    # A session with no passing test is not a passing gate, however clean the
    # exit code looks. Counted from actual call-phase passes, not from what
    # was collected.
    if getattr(config, "_release_gate_passed", 0) == 0:
        violations.append(
            "no test passed: a run that verified nothing is not a green gate")

    if violations and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _enabled(config):
        return
    violations = _violations(config)
    passed = getattr(config, "_release_gate_passed", 0)

    if not violations:
        terminalreporter.write_sep(
            "=", f"release gate satisfied: {passed} passed, no skips, no xfails",
            green=True)
        return

    terminalreporter.write_sep("=", "RELEASE GATE FAILED", red=True)
    for v in dict.fromkeys(violations):      # de-duplicated, order preserved
        terminalreporter.write_line(f"  - {v}")
    # pytest_sessionfinish already forced a non-zero exit status.
