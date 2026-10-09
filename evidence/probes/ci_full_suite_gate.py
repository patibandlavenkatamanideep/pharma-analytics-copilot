#!/usr/bin/env python3
"""Does CI's "Full test suite" step notice tests that skipped?

The security and ingestion steps run under --release-gate: a skip fails
them. The full-suite step runs everything else, among it the tests that
need the coherent-market fixture database -- the period-over-period (k-07)
regressions of 7 October among them. If that database were built but left empty, they
would skip, and a step that passes on skips would stay green.

This reads the step's command from .github/workflows/ci.yml as written,
points it at one fixture-dependent module, and runs it twice:

| Run | Fixture database | Expected |
|---|---|---|
| 1 | one with the schema and no data (a build that created it and did not load it) | non-zero: the step notices |
| 2 | the real one | exit 0 |

A database that does not exist is not the case to test: those tests then
error, and the step fails anyway. An empty one makes them skip.

Like gate_requires_its_databases.py, a verification probe: it exits 0 when
the step behaves correctly, 1 when a run of the step could pass with its
tests skipped. Nothing is created, dropped or changed: run 1 reads an
existing empty database (default: PAC_FRESHTEST_DB, which holds the schema
and no data), after checking that its calendar is empty.

    python3 evidence/probes/ci_full_suite_gate.py [--empty-db NAME]
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET = "tests/integration/test_period_over_period_fixture.py"


def step_command() -> list[str]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    step = next(s for s in workflow["jobs"]["test"]["steps"] if s.get("name") == "Full test suite")
    argv = shlex.split(step["run"])
    assert argv[:3] == ["python", "-m", "pytest"] and "tests" in argv, argv
    # The same flags, one fixture-dependent module instead of the whole tree.
    return [sys.executable, *argv[1:argv.index("tests")], TARGET, *argv[argv.index("tests") + 1:]]


def run(argv: list[str], fixture_db: str | None) -> dict:
    env = dict(os.environ)
    if fixture_db:
        env["PAC_FIXTURE_DB"] = fixture_db
    proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    counts = dict((k, int(n)) for n, k in re.findall(r"(\d+) (passed|skipped|failed|error)", tail[0]))
    return {"exit": proc.returncode, "counts": counts}


def main() -> int:
    import argparse

    import psycopg

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--empty-db", default=os.environ.get("PAC_FRESHTEST_DB"))
    args = ap.parse_args()
    if not args.empty_db:
        raise SystemExit("name an empty database: --empty-db or PAC_FRESHTEST_DB")
    with psycopg.connect(dbname=args.empty_db) as conn:
        if conn.execute("SELECT count(*) FROM app_ref.calendar").fetchone()[0]:
            raise SystemExit(f"{args.empty_db} has a calendar; it is not an empty database")
    argv = step_command()
    result = {"step": " ".join(["python", *argv[1:]]),
              "empty_fixture_db": run(argv, fixture_db=args.empty_db),
              "with_fixture_db": run(argv, fixture_db=None)}
    ok = result["empty_fixture_db"]["exit"] != 0 and result["with_fixture_db"]["exit"] == 0
    result["step_notices_skipped_tests"] = ok
    print(json.dumps(result))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
