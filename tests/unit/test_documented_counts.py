"""No document may state a test count that is not true.

At the point these were written, four documents disagreed about the size of
the suite: README.md said 368, DESIGN.md said 148, docs/REQUIREMENTS.md said
377, docs/EVALUATION.md said 148. The real number was 503. Nobody had lied;
each number was correct on the day it was typed, and there was nothing to
notice when it stopped being correct.

A reader cannot tell which of four numbers is current, so all four stop
carrying information. The repair is one measured source --
docs/TEST_INVENTORY.md -- plus this test, which fails when anything drifts
away from it.

Two documents are deliberately **out of scope**:

* ``docs/REMEDIATION.md`` and ``docs/EVALUATION.md`` are dated logs. Their
  numbers describe runs that happened, and restating them would be a
  fabrication rather than a correction (working-record assumption 5).
* ``docs/PRODUCTION_UPGRADE.md`` is the same kind of record for this work.

Everything a reader would take as present tense is in scope.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from collections import Counter

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
INVENTORY = ROOT / "docs" / "TEST_INVENTORY.md"

#: Present-tense documents. A count here is a claim about now.
GOVERNED = ["README.md", "DESIGN.md", "docs/REQUIREMENTS.md"]

#: "503 tests", "115 passing", "148 passed".
COUNT_CLAIM = re.compile(r"(\d[\d,]*)\s+(?:tests?|passing|passed)\b")

#: A test file named on the same line, which makes the claim checkable
#: against that file rather than against a suite total.
FILE_MENTION = re.compile(r"[\w./-]*\btest[\w.-]*\.(?:py|jsx|js)\b")
DIR_MENTION = re.compile(r"\b(tests/\w+|web/src/__tests__|web/e2e)/")

JS_TEST = re.compile(r"^\s*(?:it|test)\(", re.M)


@pytest.fixture(scope="module")
def collected() -> Counter:
    """Every pytest node id, counted by file. One subprocess, not thirty."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
         "-p", "no:randomly"],
        capture_output=True, text=True, cwd=ROOT)
    counts: Counter = Counter()
    for line in proc.stdout.splitlines():
        if "::" in line and line.startswith("tests/"):
            counts[line.split("::", 1)[0]] += 1
    assert counts, f"collected nothing:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    return counts


def count_under(collected: Counter, prefix: str) -> int:
    return sum(n for path, n in collected.items() if path.startswith(prefix))


def js_tests(relative: str) -> int:
    """Test cases in a JS test file, or in every test file under a directory."""
    path = ROOT / relative
    files = (sorted(path.glob("*.test.jsx")) + sorted(path.glob("*.spec.js"))
             if path.is_dir() else [path])
    return sum(len(JS_TEST.findall(f.read_text())) for f in files)


# ---------------------------------------------------------------------------
# The inventory itself
# ---------------------------------------------------------------------------

INVENTORY_ROWS = [
    pytest.param("tests/unit", r"`pytest tests/unit -q`", id="unit"),
    pytest.param("tests/integration", r"`pytest tests/integration -q`",
                 id="integration"),
    pytest.param("tests/security", r"`pytest tests/security -q --release-gate",
                 id="security"),
]


@pytest.mark.parametrize("prefix,marker", INVENTORY_ROWS)
def test_the_inventory_states_the_collected_count(collected, prefix, marker):
    """The number in the table is what pytest collects, not what anyone
    remembers."""
    row = next((line for line in INVENTORY.read_text().splitlines()
                if marker in line), None)
    assert row is not None, f"no inventory row for {prefix}"

    stated = int(row.split("|")[3].strip().replace(",", ""))

    assert stated == count_under(collected, prefix), (
        f"docs/TEST_INVENTORY.md says {stated} for {prefix}, pytest collects "
        f"{count_under(collected, prefix)}")


def test_the_inventory_total_is_the_sum_of_its_parts(collected):
    """A total typed independently of the rows above it is a fifth number to
    drift."""
    row = next(line for line in INVENTORY.read_text().splitlines()
               if "**Total (pytest)**" in line)
    stated = int(re.search(r"\*\*(\d[\d,]*)\*\*", row).group(1).replace(",", ""))

    parts = sum(count_under(collected, p) for p, _ in
                (("tests/unit", 0), ("tests/integration", 0), ("tests/security", 0)))

    assert stated == parts == sum(collected.values())


def test_the_inventory_matches_the_release_gate_floor(collected):
    """--min-tests is the number CI enforces. If the inventory and the
    workflow disagree, one of them is describing a suite that does not
    exist."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    floor = int(re.search(r'PAC_SECURITY_MIN_TESTS:\s*"(\d+)"', workflow).group(1))

    assert floor == count_under(collected, "tests/security"), (
        "the CI floor and the security suite have drifted apart")


def test_the_component_floor_is_the_component_suite():
    """CI fails a component run that executed fewer tests than the floor.
    The floor must be the number the files define, or a deleted test would
    pass unnoticed (floor too low) or every run would fail (too high)."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    floor = int(re.search(r'PAC_COMPONENT_MIN_TESTS:\s*"(\d+)"', workflow).group(1))
    assert floor == js_tests("web/src/__tests__")


@pytest.mark.parametrize("relative,expected_in_doc", [
    ("web/src/__tests__", "component"),
    ("web/e2e", "end to end"),
])
def test_the_inventory_states_the_browser_counts(relative, expected_in_doc):
    row = next(line for line in INVENTORY.read_text().splitlines()
               if f"Browser — {expected_in_doc}" in line)
    stated = int(row.split("|")[3].strip())

    assert stated == js_tests(relative), (
        f"inventory says {stated} for {relative}, the file defines "
        f"{js_tests(relative)}")


# ---------------------------------------------------------------------------
# Every other present-tense document
# ---------------------------------------------------------------------------

def _claims(document: str) -> list[tuple[int, str, int]]:
    """(line number, line, claimed count) for each count claim."""
    out = []
    for i, line in enumerate((ROOT / document).read_text().splitlines(), 1):
        for match in COUNT_CLAIM.finditer(line):
            out.append((i, line, int(match.group(1).replace(",", ""))))
    return out


@pytest.mark.parametrize("document", GOVERNED)
def test_every_count_claim_is_currently_true(collected, document):
    """Either the line names a test file -- in which case the claim is
    checked against that file -- or the number must be one the suites
    actually produce."""
    suite_counts = {
        count_under(collected, "tests/unit"),
        count_under(collected, "tests/integration"),
        count_under(collected, "tests/security"),
        sum(collected.values()),
        js_tests("web/src/__tests__"),
        js_tests("web/e2e"),
    }

    wrong: list[str] = []
    for lineno, line, claimed in _claims(document):
        named = _resolve_named_target(collected, line)
        if named is not None:
            if claimed != named[1]:
                wrong.append(
                    f"{document}:{lineno} claims {claimed} for {named[0]}, "
                    f"which has {named[1]}")
        elif claimed not in suite_counts:
            wrong.append(
                f"{document}:{lineno} claims {claimed}, which is not a count "
                f"any suite currently produces {sorted(suite_counts)}")

    assert not wrong, (
        "documented test counts have drifted:\n  " + "\n  ".join(wrong)
        + "\n\ndocs/TEST_INVENTORY.md is the measured source; point at it "
          "rather than restating a number.")


def _resolve_named_target(collected: Counter, line: str):
    """If the line names a test file or directory, return (name, count)."""
    file_match = FILE_MENTION.search(line)
    if file_match:
        name = file_match.group(0).lstrip("`")
        if name.endswith((".jsx", ".js")):
            for candidate in ("web/src/__tests__", "web/e2e"):
                path = ROOT / candidate / pathlib.Path(name).name
                if path.exists():
                    return name, js_tests(f"{candidate}/{path.name}")
            return None
        hits = {p: n for p, n in collected.items()
                if p.endswith(pathlib.Path(name).name)}
        if len(hits) == 1:
            return name, next(iter(hits.values()))
        return None

    dir_match = DIR_MENTION.search(line)
    if dir_match:
        name = dir_match.group(1)
        if name.startswith("tests/"):
            return name, count_under(collected, name)
        if name == "web/src/__tests__":
            return name, js_tests("web/src/__tests__")
        if name == "web/e2e":
            return name, js_tests("web/e2e")
    return None


@pytest.mark.parametrize("document", GOVERNED)
def test_a_present_tense_document_points_at_the_inventory(document):
    """So a reader who wants the number knows where the current one lives."""
    assert "TEST_INVENTORY.md" in (ROOT / document).read_text(), (
        f"{document} states counts without pointing at the measured source")


def test_the_dated_snapshots_say_they_are_dated():
    """REMEDIATION and EVALUATION keep their original numbers. That is only
    honest if a reader is told they are historical."""
    for document, needle in (
        ("docs/EVALUATION.md", "dated snapshot"),
        ("docs/REMEDIATION.md", "TEST_INVENTORY.md"),
    ):
        assert needle in (ROOT / document).read_text(), (
            f"{document} reads as current but is not")


MIN_TESTS_FLAG = re.compile(r"--min-tests\s+(\d+)")


@pytest.mark.parametrize("document", GOVERNED + ["docs/RUNBOOK.md", "docs/TEST_INVENTORY.md"])
def test_every_documented_gate_command_uses_the_ci_floor(document):
    """A copied command is a claim too. When the floor rose from 115 to 120,
    seven places carried the old value; a reader running one of them would
    enforce a weaker gate than CI does and believe it was the same one."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    floor = int(re.search(r'PAC_SECURITY_MIN_TESTS:\s*"(\d+)"', workflow).group(1))
    stale = [(i, int(m.group(1)))
             for i, line in enumerate((ROOT / document).read_text().splitlines(), 1)
             for m in MIN_TESTS_FLAG.finditer(line) if int(m.group(1)) != floor]
    assert not stale, f"{document}: --min-tests values {stale} differ from the CI floor {floor}"
