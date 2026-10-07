"""The evidence index's inputs stay complete, so a count can never be read as
more than it establishes (scripts/evidence_index.py).

Every test file is in exactly one category of evidence/test_categories.json --
otherwise its tests would be counted under no meaning, or under two -- and
every file and commit the defect ledger names exists.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_every_test_file_is_in_exactly_one_category():
    mapping = json.loads((ROOT / "evidence" / "test_categories.json").read_text())
    listed = Counter(f for spec in mapping["categories"].values() for f in spec["files"])
    present = {str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("test_*.py")}
    assert not [f for f, n in listed.items() if n > 1], "a file is in two categories"
    assert sorted(present - set(listed)) == [], "uncategorised test files"
    assert sorted(set(listed) - present) == [], "categories name files that do not exist"


def test_every_ledger_reference_exists():
    ledger = json.loads((ROOT / "evidence" / "ledger.json").read_text())
    missing = []
    for defect in ledger["defects"]:
        for commit in defect["fix_commits"]:
            if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", commit + "^{commit}"],
                              capture_output=True).returncode != 0:
                missing.append(commit)
        missing += [f for f in defect["regression_files"] if not (ROOT / f).exists()]
        for rep in defect["reproductions"]:
            for key in ("artifact", "junit", "result"):
                ref = rep.get(key)
                if ref and not ref.startswith("hosted-ci:") and not (ROOT / ref).exists():
                    missing.append(ref)
    assert missing == []
