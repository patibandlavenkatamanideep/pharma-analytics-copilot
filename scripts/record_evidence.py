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
SCHEMA_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
#
# Two mechanisms, because they fail in different directions.
#
# 1. An ALLOWLIST for environment overrides. The first version matched names
#    against a pattern of secret-sounding words, which is a denylist wearing
#    different clothes: PAC_EVALUATOR_LOGIN, BEDROCK_BEARER, PGPASSFILE and
#    AWS_SESSION_* all describe credentials and none of them match. A
#    denylist is only as good as the last name somebody thought of, and the
#    cost of being wrong is a credential committed to the repository. So the
#    default is now redaction, and a variable's VALUE is recorded only if it
#    appears below.
#
# 2. A VALUE scan for everything else. Most of a record is not a name/value
#    pair -- it is a summary line, an exception message, a command argument --
#    and none of that can be allowlisted. Credential-SHAPED text is stripped
#    from every string in the record, at any depth.

#: Environment overrides whose values describe WHAT was run, never WHERE or
#: AS WHOM. Anything absent from this set is recorded as present-but-redacted,
#: which is the fact the reader needs -- that the run was configured -- without
#: the value.
ENV_VALUE_ALLOWLIST = frozenset({
    "CI",
    "PAC_LLM_PROVIDER",
    "PAC_DB_NAME",
    "PAC_DB_PORT",
    "PAC_EVAL_SUITE",
    "PAC_EVAL_LIMIT",
    "PAC_SECURITY_MIN_TESTS",
    "PAC_STRICT_SECURITY",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "PYTHONHASHSEED",
    "PYTHONDONTWRITEBYTECODE",
    "TZ",
})

#: Keys whose values are removed wherever they appear in the record, at any
#: depth -- not only in the environment block. Kept as a second line of
#: defence for structures that are not the environment: a nested attempt
#: record, a parsed config, an artifact description.
SECRET_NAME = re.compile(
    r"PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|DSN|CONN|AUTH|COOKIE|SESSION|"
    r"PASSFILE|LOGIN|BEARER", re.I)

#: Credential-shaped VALUES. A password reaches a record as a value too --
#: inside a connection string in an exception message, in a summary line, in
#: a command argument. Matched against every string anywhere in the record.
SECRET_VALUE_PATTERNS = (
    # libpq keyword/value form, as make_conninfo emits it. The value may be
    # single-quoted and may contain escaped quotes.
    re.compile(r"password\s*=\s*(?:'(?:[^'\\]|\\.)*'|\S+)", re.I),
    # A URL connection string carrying a password, for any driver.
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@]+@", re.I),
    # AWS-shaped credentials.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"aws_secret_access_key\s*[=:]\s*\S+", re.I),
    re.compile(r"\baws_session_token\s*[=:]\s*\S+", re.I),
    # Bearer tokens, API keys and session cookies.
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.I),
    re.compile(r"\bsk-ant-[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bpac_session\s*=\s*\S+", re.I),
)

REDACTED = "<redacted>"
#: Distinct from REDACTED so a reader can tell "this was set, value withheld"
#: from "this string contained something credential-shaped".
WITHHELD = "<set, value withheld>"


def _redact_env(overrides: dict[str, str]) -> dict[str, str]:
    """Allowlist: record the value only for names known to be safe."""
    return {
        k: (_scrub(v) if k in ENV_VALUE_ALLOWLIST else WITHHELD)
        for k, v in overrides.items()
    }


def _scrub(value: Any) -> Any:
    """Recursively remove credential-shaped values, at any depth.

    Applied to the whole record before it is written, so a secret cannot
    reach disk through a field nobody thought to redact -- a captured
    exception, a summary line, a nested mapping.
    """
    if isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and SECRET_NAME.search(k)
                else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20
        ).stdout.strip() or None
    except Exception:
        return None


def _dirty_paths() -> list[str]:
    """Paths git reports as changed.

    Parsed from ``--porcelain -z`` rather than from ``_git()``, which strips
    the output: the first entry's two-character status field starts with a
    space for an unstaged change, so stripping ate it and every first path
    was recorded one character short -- ``pp/config.py``. A record that
    misstates which files were dirty is exactly the kind of quiet
    inaccuracy this directory exists to prevent. ``-z`` also survives paths
    containing spaces, which the line-split form quoted and mangled.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=ROOT, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    paths: list[str] = []
    entries = iter(out.split("\0"))
    for entry in entries:
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            # A rename entry is followed by its origin path as a separate
            # NUL-terminated field; consume it so it is not read as a status.
            next(entries, None)
        paths.append(path)
    return sorted(paths)


def _application() -> dict[str, Any]:
    """Which bytes ran. A commit SHA alone identifies them only when the tree
    is clean; when it is not, each changed file's digest is recorded too, so
    a record made on an uncommitted tree still names exactly what was tested
    (review of 1 October 2026). Release checks use --require-clean."""
    paths = _dirty_paths()
    digests: dict[str, str | None] = {}
    for path in paths[:50]:
        target = ROOT / path
        digests[path] = (hashlib.sha256(target.read_bytes()).hexdigest()
                         if target.is_file() else None)          # deleted, or a directory
    return {
        "sha": _git("rev-parse", "HEAD"),
        "tree": _git("rev-parse", "HEAD^{tree}"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_clean": not paths,
        "dirty_paths": paths[:50],
        "dirty_digests": digests or None,
    }


def _image(ref: str | None, cli: str | None) -> dict[str, Any] | None:
    """The container image a check ran against, as the engine reports it:
    id, repository digest where it has one, platform and revision label.
    A tag names whatever was last built; the id names these bytes."""
    if not ref:
        return None
    import os
    cli = cli or os.environ.get("CONTAINER_CLI") or "podman"
    try:
        proc = subprocess.run([cli, "image", "inspect", ref], capture_output=True, text=True,
                              timeout=60)
        info = json.loads(proc.stdout)[0] if proc.returncode == 0 else None
    except Exception:
        info = None
    if not info:
        return {"ref": ref, "inspected_with": cli, "id": None, "digest": None,
                "platform": None, "revision_label": None}
    labels = (info.get("Config") or {}).get("Labels") or info.get("Labels") or {}
    digests = info.get("RepoDigests") or []
    return {"ref": ref, "inspected_with": cli, "id": info.get("Id"),
            "digest": info.get("Digest") or (digests[0].split("@", 1)[-1] if digests else None),
            "platform": f"{info.get('Os')}/{info.get('Architecture')}",
            "revision_label": labels.get("org.opencontainers.image.revision")}


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
    ap.add_argument("--require-clean", action="store_true",
                    help="a release check: refuse to run on an uncommitted tree, and record "
                         "the refusal as blocked")
    ap.add_argument("--image", default=None,
                    help="the container image this check ran against; its id, digest, "
                         "platform and revision label are recorded")
    ap.add_argument("--container-cli", default=None, help="podman (default) or docker")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    argv = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    overrides = dict(pair.split("=", 1) for pair in args.env if "=" in pair)

    exit_code: int | None = None
    duration: float | None = None
    stdout = ""
    refused = args.require_clean and bool(_dirty_paths())
    if refused:
        args.status = "blocked"
        args.blocked_reason = ("--require-clean: the working tree has uncommitted changes, so "
                               "the commit does not identify what would run")
        print(args.blocked_reason, file=sys.stderr)
    elif argv:
        import os
        env = {**os.environ, **overrides}
        started = time.monotonic()
        proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
        duration = round(time.monotonic() - started, 2)
        exit_code = proc.returncode
        stdout = (proc.stdout or "") + (proc.stderr or "")
        # Scrubbed on the way to the terminal as well: a CI log is as
        # durable as a committed file, and often more widely readable.
        sys.stdout.write(_scrub(stdout[-4000:]))

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
    image = _image(args.image, args.container_cli)
    if image is not None:
        record["image"] = image

    # Scrubbed as a whole, after assembly: the summary line and any captured
    # error come from a child process and are not under this script's
    # control.
    record = _scrub(record)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(f"\nevidence -> {args.out}  [{status}]")
    return 2 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
