"""The release gate must not report success for a run that proved nothing.

`pytest tests/security -q` is documented as the gate. On its own it exits 0
when every test skips, which is what happens with no database loaded -- a
green tick for a run that checked nothing. These tests drive pytest in a
subprocess and assert the gate closes both holes.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

CONFTEST = '''
import sys
sys.path.insert(0, %r)
pytest_plugins = ["tests.release_gate"]
'''


@pytest.fixture
def gate(pytester):
    import pathlib

    root = str(pathlib.Path(__file__).resolve().parent.parent.parent)
    pytester.makeconftest(CONFTEST % root)
    return pytester


def test_a_skipped_test_fails_the_gate(gate):
    gate.makepyfile(test_skipper="""
        import pytest

        @pytest.mark.skipif(True, reason="needs a loaded database")
        def test_needs_database():
            assert False
    """)

    without = gate.runpytest("-q")
    without.assert_outcomes(skipped=1)
    assert without.ret == 0, "plain pytest already treats an all-skipped run as success"

    with_gate = gate.runpytest("-q", "--release-gate")
    # A skip is decided during setup, so turning it into a non-pass reports it
    # as an error rather than a failure. What matters is the non-zero exit and
    # a reason that says the test did not run.
    with_gate.assert_outcomes(errors=1, passed=0, skipped=0)
    assert with_gate.ret != 0
    with_gate.stdout.fnmatch_lines(["*nothing was verified*"])


def test_passing_tests_are_unaffected_by_the_gate(gate):
    gate.makepyfile(test_ok="""
        def test_one(): assert True
        def test_two(): assert True
    """)
    result = gate.runpytest("-q", "--release-gate", "--min-tests", "2")
    result.assert_outcomes(passed=2)
    assert result.ret == 0


def test_too_few_tests_fails_the_gate(gate):
    """A narrowed or mistyped selection must not pass as a full run."""
    gate.makepyfile(test_few="""
        def test_one(): assert True
    """)
    result = gate.runpytest("-q", "--release-gate", "--min-tests", "90")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*1 tests collected, expected at least 90*"])


def test_an_empty_selection_fails_the_gate(gate):
    gate.makepyfile(test_none="""
        def helper_not_a_test(): pass
    """)
    result = gate.runpytest("-q", "--release-gate", "--min-tests", "1")
    assert result.ret != 0, "a selection that matched no tests reported success"
