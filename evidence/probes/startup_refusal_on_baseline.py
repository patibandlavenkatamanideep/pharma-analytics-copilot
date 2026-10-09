#!/usr/bin/env python3
"""Would the baseline CI's startup-refusal steps have passed against the baseline app?

    python3 evidence/probes/startup_refusal_on_baseline.py <baseline-commit> [--out result.json]

A detached worktree at <baseline-commit> runs `uvicorn app.api.main:app` the way the
baseline CI step did, with its 25-second `timeout`:

* no database: nothing listens on the configured port;
* owner credential: PAC_ENVIRONMENT=cloud with the owner password present.

Each output is judged with the baseline workflow's own grep (`connection refused|
boundary`, case-insensitive; `OWNER credential`). Prints one JSON object: per case the
exit status (124 if still running at the deadline), seconds, whether the grep matched,
and whether the CI step would have passed -- to --out if given -- then a one-line
verdict, which an evidence record keeps as its summary. Exits 1 if either step would have failed,
which is the expected outcome on code without the fix. The log text itself is not
printed; only the structured reasons it contains.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
PY = os.environ.get("PY", str(pathlib.Path.home() / ".venvs/pac-release/bin/python"))
DEADLINE = 25
STILL_RUNNING = 124


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(worktree: pathlib.Path, **env: str) -> tuple[int, float, str]:
    environ = {**os.environ, "PYTHONPATH": str(worktree), "PAC_LLM_PROVIDER": "offline", **env}
    environ.pop("PAC_OTEL_ENDPOINT", None)
    started = time.monotonic()
    try:
        proc = subprocess.run([PY, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
                               "--port", str(free_port())], cwd=worktree, env=environ,
                              capture_output=True, text=True, timeout=DEADLINE)
        code, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as running:
        code = STILL_RUNNING
        out = "".join(x.decode() if isinstance(x, bytes) else (x or "")
                      for x in (running.stdout, running.stderr))
    return code, round(time.monotonic() - started, 2), out


def reasons(out: str) -> list[str]:
    found = []
    for line in out.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("event") == "startup.refused":
            found.append(str(record.get("reason")))
    return found


def main() -> int:
    base = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", sys.argv[1] + "^{commit}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    worktree = pathlib.Path(tempfile.mkdtemp(prefix="pac-startup-"))
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(worktree), base],
                   check=True, capture_output=True)
    try:
        if (REPO / ".env").exists():
            (worktree / ".env").write_bytes((REPO / ".env").read_bytes())
        cases = {}
        code, secs, out = run(worktree, PAC_DB_HOST="127.0.0.1", PAC_DB_PORT=str(free_port()))
        matched = re.search(r"connection refused|boundary", out, re.I) is not None
        cases["no_database"] = {"exit": code, "seconds": secs, "baseline_grep_matched": matched,
                                "structured_reasons": reasons(out), "ci_step_would_pass": matched}
        code, secs, out = run(worktree, PAC_ENVIRONMENT="cloud")
        matched = "OWNER credential" in out
        cases["owner_credential"] = {"exit": code, "seconds": secs, "baseline_grep_matched": matched,
                                     "structured_reasons": reasons(out), "ci_step_would_pass": matched}
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(worktree)],
                       capture_output=True)
        subprocess.run(["git", "-C", str(REPO), "worktree", "prune"], capture_output=True)
    result = {"baseline": base, "deadline_s": DEADLINE, "cases": cases}
    if "--out" in sys.argv:
        pathlib.Path(sys.argv[sys.argv.index("--out") + 1]).write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    print(f"baseline {base[:7]}: " + "; ".join(
        f"{name}: exit {c['exit']} after {c['seconds']} s, baseline grep matched {c['baseline_grep_matched']}"
        for name, c in cases.items()))
    return 0 if all(c["ci_step_would_pass"] for c in cases.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
