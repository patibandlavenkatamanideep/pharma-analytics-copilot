"""The invariant map names real tests, and its report is current.

evidence/invariants.json says which tests check each business invariant
against an independent oracle; docs/COVERAGE_BY_INVARIANT.md is rendered from
it. A renamed or deleted test would leave an invariant claiming coverage it
no longer has.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_every_referenced_test_exists():
    data = json.loads((ROOT / "evidence" / "invariants.json").read_text())
    missing = []
    for inv in data["invariants"]:
        assert inv["tests"], f"{inv['id']} cites no test"
        for ref in inv["tests"]:
            path, name = ref.split("::")
            text = (ROOT / path).read_text() if (ROOT / path).exists() else ""
            if not re.search(rf"^def {re.escape(name)}\(", text, re.M):
                missing.append(ref)
    assert missing == []


def test_the_coverage_report_is_rendered_from_the_map():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "coverage_report.py"), "--check"])
    assert r.returncode == 0, "docs/COVERAGE_BY_INVARIANT.md is stale: run scripts/coverage_report.py"
