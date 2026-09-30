#!/usr/bin/env python3
"""Run a command and record what it actually established.

Every claim in docs/PRODUCTION_UPGRADE.md points at a record written by this
script. The point is not logging -- it is that a reviewer can tell, for any
claim, which revision it was measured on, against which data, in which
provider mode, and what the run does NOT establish.

  python3 scripts/record_evidence.py --claim "baseline suite passes" \
      --out evidence/runs/baseline.json -- python3 -m pytest tests -q

Unknown versions are written as null. An invented string would be worse than
an absent one, because it reads as a measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "1.0.0"

# Any environment variable whose name matches is recorded as present, never by
# value. Checked against the NAME, so a new secret does not need a code change
# to be protected.
SECRET_NAME = re.compile(
    r"PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|DSN|CONN|AUTH", re.I)


def _redact_env(overrides: dict[str, str]) -> dict[str, str]:
    return {
        k: ("<redacted>" if SECRET_NAME.search(k) else v)
        for k, v in overrides.items()
    }


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20
        ).stdout.strip() or None
    except Exception:
        return None


def _application() -> dict[str, Any]:
    dirty = _git("status", "--porcelain") or ""
    paths = [line[3:] for line in dirty.splitlines() if line.strip()]
    return {
        "sha": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_clean": not paths,
        "dirty_paths": paths[:50],
    }


def _versions() -> dict[str, Any]:
    """Contract versions, with null where the contract is not yet versioned."""
    out: dict[str, Any] = {
        "metric_registry": None, "metric_registry_digest": None,
        "policy": None, "schema_contract": None, "mapping": None,
        "classification_rules": None,
        # Not yet versioned in code. Explicitly null rather than omitted, so
        # the gap is visible in every record until it is closed.
        "prompt": None, "planner": None, "graph": None,
        "suite": None, "oracle": None,
    }
    sys.path.insert(0, str(ROOT))
    try:
        from app.analytics.registry import get_registry
        r = get_registry()
        out["metric_registry"] = r.version
        out["metric_registry_digest"] = r.digest
    except Exception:
        pass
    for key, module, attr in (
        ("policy", "app.pipeline", "POLICY_VERSION"),
        ("schema_contract", "app.data.schema_contract", "CONTRACT_VERSION"),
        ("mapping", "app.data.manifest", "MAPPING_VERSION"),
        ("classification_rules", "app.data.classification", "RULE_VERSION"),
        ("prompt", "app.llm.planner", "PROMPT_VERSION"),
        ("planner", "app.llm.planner", "PLANNER_CONTRACT_VERSION"),
        ("graph", "app.graph", "GRAPH_VERSION"),
    ):
        try:
            mod = __import__(module, fromlist=[attr])
            out[key] = getattr(mod, attr)
        except Exception:
            pass
    return out


def _dataset(database: str | None) -> dict[str, Any] | None:
    """The published snapshot, by NAME only — never a DSN."""
    if not database:
        return None
    sys.path.insert(0, str(ROOT))
    try:
        import os
        os.environ["PAC_DB_NAME"] = database
        from app.config import get_settings
        get_settings.cache_clear()
        from app.db import close_pools, owner_transaction
        with owner_transaction() as cur:
            cur.execute(
                "SELECT dataset_id, load_mode, row_counts, reporting_anchor, "
                "       source_hashes "
                "FROM app_meta.dataset_manifest WHERE load_state = 'published' "
                "ORDER BY published_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        close_pools()
        if row is None:
            return {"dataset_id": None, "load_mode": None, "fingerprint": None,
                    "row_counts": None, "reporting_anchor": None, "database": database}
        # A content fingerprint over the source hashes and row counts, so the
        # record identifies the DATA rather than the generated dataset id.
        blob = json.dumps(
            {"h": row["source_hashes"], "c": row["row_counts"]}, sort_keys=True, default=str)
        return {
            "dataset_id": row["dataset_id"],
            "load_mode": row["load_mode"],
            "fingerprint": hashlib.sha256(blob.encode()).hexdigest()[:24],
            "row_counts": row["row_counts"],
            "reporting_anchor": row["reporting_anchor"],
            "database": database,
        }
    except Exception as exc:
        return {"dataset_id": None, "load_mode": None, "fingerprint": None,
                "row_counts": None, "reporting_anchor": None,
                "database": f"{database} (unreadable: {type(exc).__name__})"}


def _postgres_version() -> str | None:
    try:
        out = subprocess.run(["psql", "-V"], capture_output=True, text=True, timeout=10).stdout
        return out.strip() or None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim", default=None, help="the sentence this run is evidence for")
    ap.add_argument("--out", required=True, help="where to write the record")
    ap.add_argument("--provider-mode", default="offline", choices=("offline", "fake", "bedrock"))
    ap.add_argument("--database", default=None, help="logical database name to fingerprint")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--limit", action="append", default=[],
                    help="what this run does NOT establish; repeatable")
    ap.add_argument("--env", action="append", default=[],
                    help="NAME=VALUE applied to the child; secret-looking names are redacted")
    ap.add_argument("--status", default=None,
                    choices=("passed", "failed", "blocked", "not_run", "not_applicable"),
                    help="override the status; default is derived from the exit code")
    ap.add_argument("--blocked-reason", default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    argv = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    overrides = dict(pair.split("=", 1) for pair in args.env if "=" in pair)

    exit_code: int | None = None
    duration: float | None = None
    stdout = ""
    if argv:
        import os
        env = {**os.environ, **overrides}
        started = time.monotonic()
        proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
        duration = round(time.monotonic() - started, 2)
        exit_code = proc.returncode
        stdout = (proc.stdout or "") + (proc.stderr or "")
        sys.stdout.write(stdout[-4000:])

    if args.status:
        status = args.status
    elif exit_code is None:
        status = "not_run"
    else:
        status = "passed" if exit_code == 0 else "failed"

    counts: dict[str, int] | None = None
    m = re.findall(r"(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed)", stdout)
    if m:
        counts = {}
        for n, kind in m:
            counts[kind.rstrip("s") if kind != "passed" else "passed"] = int(n)

    record = {
        "evidence_schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claim": args.claim,
        "application": _application(),
        "versions": _versions(),
        "dataset": _dataset(args.database),
        "environment": {
            "provider_mode": args.provider_mode,
            "provider_model_id": None,
            "python": platform.python_version(),
            "postgresql": _postgres_version(),
            "os": f"{platform.system()} {platform.release()} {platform.machine()}",
            "cpu_count": __import__("os").cpu_count(),
            "container": False,
            "notes": None,
        },
        "command": {
            "argv": argv,
            "cwd_relative": ".",
            "env_overrides": _redact_env(overrides) or None,
            "seed": args.seed,
            "duration_s": duration,
        },
        "outcome": {
            "status": status,
            "exit_code": exit_code,
            "counts": counts,
            "blocked_reason": args.blocked_reason,
            "summary": (stdout.strip().splitlines() or [None])[-1],
        },
        "artifacts": [],
        "limits": args.limit or None,
    }

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(f"\nevidence -> {args.out}  [{status}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
