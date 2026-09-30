#!/usr/bin/env python3
"""Reproduce: the release gate accepts a skip raised from a test body.

tests/release_gate.py converts a skip to a failure only when
report.when == "setup". A test that calls pytest.skip() inside its own body
skips at CALL phase, so the gate lets it through and the run exits 0 --
a green release gate for a test that verified nothing.

Exits 0 when the DEFECT IS PRESENT, 1 once it is fixed.
"""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

CONFTEST = f'import sys\nsys.path.insert(0, {str(ROOT)!r})\npytest_plugins = ["tests.release_gate"]\n'
TEST = 'import pytest\n\ndef test_skips_from_its_own_body():\n    pytest.skip("prerequisite missing, decided at call time")\n'

with tempfile.TemporaryDirectory() as tmp:
    d = pathlib.Path(tmp)
    (d / "conftest.py").write_text(CONFTEST)
    (d / "test_probe.py").write_text(TEST)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(d), "-q", "--release-gate", "--min-tests", "1"],
        capture_output=True, text=True, cwd=tmp,
    )

print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(no output)")
print(f"exit code: {proc.returncode}")
if proc.returncode == 0:
    print("DEFECT PRESENT: the gate passed a run in which nothing was verified.")
    sys.exit(0)
print("FIXED: the gate rejected the call-phase skip.")
sys.exit(1)
