"""The release gate must reject every way a run can verify nothing.

The first version of the gate checked one thing: a skip reported during
**setup**. That covers an unavailable fixture and nothing else. A test that
calls ``pytest.skip()`` in its own body skips at CALL phase; a module that
calls ``pytest.skip(allow_module_level=True)`` produces no items at all.
Both exited 0 under ``--release-gate``.

These run pytest in a subprocess, because the thing under test is the exit
code of a pytest run, and an in-process assertion about outcomes would not
catch a plugin that reports violations but never changes the status.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

CONFTEST = textwrap.dedent(f"""
    import sys
    sys.path.insert(0, {str(ROOT)!r})
    pytest_plugins = ["tests.release_gate"]
""")


def run_gate(tmp_path: pathlib.Path, body: str, *extra: str,
             filename: str = "test_probe.py") -> subprocess.CompletedProcess:
    """Run one throwaway test file under the strict gate, in a subprocess."""
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / filename).write_text(textwrap.dedent(body))
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q",
         "--release-gate", "--min-tests", "1", *extra],
        capture_output=True, text=True, cwd=tmp_path,
    )


# ---------------------------------------------------------------------------
# Skips, in every phase
# ---------------------------------------------------------------------------

def test_a_setup_phase_skip_fails_the_gate(tmp_path):
    """An unavailable fixture. This is the case the gate always caught."""
    r = run_gate(tmp_path, """
        import pytest

        @pytest.fixture
        def needs_database():
            pytest.skip("no database")

        def test_uses_it(needs_database):
            assert True
    """)
    assert r.returncode != 0, r.stdout
    assert "RELEASE GATE FAILED" in r.stdout


def test_a_call_phase_skip_fails_the_gate(tmp_path):
    """The hole. pytest.skip() inside a test body used to exit 0."""
    r = run_gate(tmp_path, """
        import pytest

        def test_decides_at_runtime():
            pytest.skip("prerequisite missing, decided at call time")
    """)
    assert r.returncode != 0, f"a call-phase skip passed the gate:\n{r.stdout}"
    assert "skipped during call" in r.stdout


def test_a_skipif_marker_fails_the_gate(tmp_path):
    r = run_gate(tmp_path, """
        import pytest

        @pytest.mark.skipif(True, reason="marked out")
        def test_marked():
            assert False
    """)
    assert r.returncode != 0, r.stdout


def test_a_module_level_skip_fails_the_gate(tmp_path):
    """No items are produced at all, so --min-tests sees a smaller
    collection rather than a violation."""
    r = run_gate(tmp_path, """
        import pytest
        pytest.skip("whole module opted out", allow_module_level=True)

        def test_never_runs():
            assert True
    """)
    assert r.returncode != 0, f"a module-level skip passed the gate:\n{r.stdout}"


# ---------------------------------------------------------------------------
# Expected failures
# ---------------------------------------------------------------------------

def test_an_unapproved_xfail_fails_the_gate(tmp_path):
    """A known-failing security test is a known-failing security
    requirement. It belongs outside the mandatory gate."""
    r = run_gate(tmp_path, """
        import pytest

        @pytest.mark.xfail(reason="known broken")
        def test_known_broken():
            assert False
    """)
    assert r.returncode != 0, f"an xfail passed the gate:\n{r.stdout}"
    assert "xfail" in r.stdout.lower()


def test_an_xpass_also_fails_the_gate(tmp_path):
    """A test marked as expected-to-fail that passes is equally unclear
    about what the requirement is."""
    r = run_gate(tmp_path, """
        import pytest

        @pytest.mark.xfail(reason="thought this was broken")
        def test_unexpectedly_works():
            assert True
    """)
    assert r.returncode != 0, r.stdout


def test_xfail_can_be_permitted_explicitly(tmp_path):
    """The escape hatch exists, is off by default, and has to be asked for."""
    r = run_gate(tmp_path, """
        import pytest

        @pytest.mark.xfail(reason="approved elsewhere")
        def test_known_broken():
            assert False

        def test_real_one():
            assert True
    """, "--allow-xfail")
    assert r.returncode == 0, r.stdout


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_an_empty_selection_fails_the_gate(tmp_path):
    r = run_gate(tmp_path, """
        def helper_not_a_test():
            pass
    """)
    assert r.returncode != 0, r.stdout


def test_a_narrowed_selection_fails_the_min_tests_guard(tmp_path):
    """A -k that quietly matches two tests out of a hundred."""
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_many.py").write_text(textwrap.dedent("""
        def test_alpha(): assert True
        def test_beta(): assert True
        def test_gamma(): assert True
    """))
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q",
         "--release-gate", "--min-tests", "3", "-k", "alpha"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert r.returncode != 0, r.stdout
    assert "collected" in (r.stdout + r.stderr)


def test_a_collection_error_fails_the_gate(tmp_path):
    """A module that cannot import must not reduce to a smaller collection."""
    r = run_gate(tmp_path, """
        import a_module_that_does_not_exist  # noqa: F401

        def test_never_runs():
            assert True
    """)
    assert r.returncode != 0, r.stdout


# ---------------------------------------------------------------------------
# The session as a whole
# ---------------------------------------------------------------------------

def test_a_session_where_nothing_passed_fails(tmp_path):
    """Zero of zero is not a green gate.

    Constructed so that nothing fails either -- every test is deselected by
    the marker expression -- which previously produced a clean exit 0.
    """
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_all_deselected.py").write_text(textwrap.dedent("""
        import pytest

        @pytest.mark.slow
        def test_one(): assert True
    """))
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: slow\n")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q",
         "--release-gate", "--min-tests", "1", "-m", "not slow"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert r.returncode != 0, f"a session that ran nothing exited 0:\n{r.stdout}"


def test_a_teardown_failure_still_fails(tmp_path):
    """Teardown failures are ordinary pytest errors; confirm the gate does
    not mask them while rewriting outcomes."""
    r = run_gate(tmp_path, """
        import pytest

        @pytest.fixture
        def breaks_on_teardown():
            yield
            raise RuntimeError("teardown exploded")

        def test_body_is_fine(breaks_on_teardown):
            assert True
    """)
    assert r.returncode != 0, r.stdout


# ---------------------------------------------------------------------------
# The gate must not fire when it is off, or on a genuinely clean run
# ---------------------------------------------------------------------------

def test_a_genuinely_clean_run_passes(tmp_path):
    r = run_gate(tmp_path, """
        def test_one(): assert True
        def test_two(): assert True
    """)
    assert r.returncode == 0, r.stdout
    assert "release gate satisfied" in r.stdout


def test_without_the_flag_a_skip_is_still_just_a_skip(tmp_path):
    """The gate is opt-in. Ordinary development runs are unaffected."""
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_probe.py").write_text(textwrap.dedent("""
        import pytest

        def test_skips():
            pytest.skip("fine outside the gate")
    """))
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert r.returncode == 0, r.stdout
    assert "RELEASE GATE" not in r.stdout


@pytest.mark.parametrize("flag", ["--release-gate"])
def test_the_gate_explains_itself(tmp_path, flag):
    """A failing gate that does not say which rule it enforced is a puzzle."""
    r = run_gate(tmp_path, """
        import pytest

        def test_skips():
            pytest.skip("missing thing")
    """)
    assert "RELEASE GATE FAILED" in r.stdout
    assert "nothing was verified" in r.stdout or "skipped during" in r.stdout
