#!/usr/bin/env python3
"""Prove the strict gate fails when a prerequisite is taken away.

A gate that cannot fail is not a gate. `tests/security` is the release
gate, and its tests skip when the disposable authorization database is
absent -- which is the correct thing for them to do during development and
the wrong thing for a release decision. Before Phase 1B, `pytest
tests/security -q` with no database exited **0**, and CI ran exactly that
command without ever building the database those tests need. The job had
been green for its entire history.

So this does not assert that the gate passes. It takes the prerequisite
away and asserts that the gate **notices**:

| Run | Prerequisite | Gate | Expected |
|---|---|---|---|
| 1 | removed | off | exit 0 -- the false green, kept here so the difference is visible |
| 2 | removed | on | **non-zero** |
| 3 | present | on | exit 0 |

Run 2 is the one that matters. Run 1 is what CI used to do.

**Exit semantics are the opposite of the Phase 0 defect probes.** Those
exit 0 while a defect is present. This is a verification probe: it exits 0
when the gate behaves correctly.

Nothing is dropped, truncated or created. The prerequisite is removed by
pointing `PAC_AUTHTEST_DB` at a database name that does not exist; the
real databases are never touched.

    python3 evidence/probes/gate_requires_its_databases.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# One small module that depends on the authtest database. The whole suite
# would prove the same thing and take four minutes; the gate's rule is
# per-outcome, so one module exercises it exactly as well.
TARGET = "tests/security/test_turn_concurrency.py"
ABSENT = "pharma_analytics_absent_probe"


def run(*extra: str, absent: bool) -> tuple[int, float]:
    env = dict(os.environ)
    if absent:
        env["PAC_AUTHTEST_DB"] = ABSENT
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-q", "-p", "no:randomly",
         *extra],
        cwd=ROOT, env=env, capture_output=True, text=True)
    return proc.returncode, round(time.monotonic() - started, 1)


def main() -> int:
    gate = ["--release-gate", "--min-tests", "5"]

    ungated_absent, t1 = run(absent=True)
    gated_absent, t2 = run(*gate, absent=True)
    gated_present, t3 = run(*gate, absent=False)

    rows = [
        ("prerequisite removed, gate off", ungated_absent, 0, t1,
         "the false green: a run that verified nothing"),
        ("prerequisite removed, gate on", gated_absent, "non-zero", t2,
         "the gate must notice"),
        ("prerequisite present, gate on", gated_present, 0, t3,
         "and must not cry wolf"),
    ]

    ok = (ungated_absent == 0 and gated_absent != 0 and gated_present == 0)

    print(f"{'run':34} {'exit':>5} {'expected':>10} {'s':>6}  note")
    for label, got, want, secs, note in rows:
        print(f"{label:34} {got:>5} {str(want):>10} {secs:>6}  {note}")

    if ok:
        print("\nOK: the gate fails without its database and passes with it.")
        return 0

    print("\nFAILED: the gate did not behave as required.")
    if gated_absent == 0:
        print("  The gate exited 0 with its prerequisite removed. It is not "
              "a gate.")
    if gated_present != 0:
        print("  The gate failed with its prerequisite present, so a green "
              "run is not achievable and the gate will be disabled.")
    if ungated_absent != 0:
        print("  Note: the ungated run also failed, so this probe no longer "
              "demonstrates the contrast it was written to show.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
