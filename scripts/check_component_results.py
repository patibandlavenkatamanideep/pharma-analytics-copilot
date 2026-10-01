#!/usr/bin/env python3
"""Fail unless a vitest run executed at least the expected number of tests,
and every one of them passed.

    npm --prefix web test -- --reporter=default --reporter=json \\
        --outputFile=component-results.json
    python3 scripts/check_component_results.py web/component-results.json 21

vitest already exits non-zero when it finds no test files, or when its
worker never starts (both observed). What it does not catch is a suite that
quietly got SMALLER -- a deleted file, an `it.skip`, a narrowed include
pattern -- which still exits 0. This is the component suite's counterpart
of the security suite's --release-gate --min-tests.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    report = json.load(open(sys.argv[1]))
    floor = int(sys.argv[2])
    total = report.get("numTotalTests", 0)
    passed = report.get("numPassedTests", 0)
    skipped = report.get("numPendingTests", 0) + report.get("numTodoTests", 0)
    failed = report.get("numFailedTests", 0)
    problems = []
    if total < floor:
        problems.append(f"{total} tests ran; at least {floor} are required")
    if skipped:
        problems.append(f"{skipped} skipped or todo")
    if failed or passed != total:
        problems.append(f"{failed} failed, {passed} of {total} passed")
    if problems:
        print("component gate FAILED: " + "; ".join(problems), file=sys.stderr)
        return 1
    print(f"component gate satisfied: {passed} passed, none skipped (floor {floor})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
