#!/usr/bin/env python3
"""One release manifest: what was tested, from which bytes, and what it found.

    python3 scripts/release_manifest.py --candidate <sha> --records 'evidence/runs/r5-cand-*.json' \\
        --image-record evidence/runs/r5-cand-image.json \\
        --trivy evidence/runs/r5-cand-trivy.json --trivy-full evidence/runs/r5-cand-trivy-full.json \\
        --trivy-db evidence/runs/r5-cand-trivy-db.json --out evidence/release/manifest.json

It connects the source (commit, tree, dependency locks, contract versions and
the prompt fingerprint), every command record of the candidate (each file's
SHA-256, the commit it measured, clean or not, its outcome), the image (with
what kind of digest each field is), the scanner (its version, its database's
dates, the gate's result and the complete counts) and the reviewed
exceptions. When HEAD is later than the candidate, the paths between them are
listed and each is classed as a build input or not: a manifest whose delta
touches a build input says so, and its records do not cover HEAD.

Nothing here runs a check; it reads the records the checks wrote. Exit 1 if a
record measured another commit, measured a dirty tree, or did not pass.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCKS = ["requirements.lock", "pyproject.toml", "web/package-lock.json", "web/package.json",
         "Dockerfile", ".dockerignore"]
#: Paths that cannot change what is built, installed, tested or run.
NOT_INPUTS = ("docs/", "evidence/", "README.md")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def sha256(path: pathlib.Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def load(path: str | None) -> dict | None:
    return json.loads((ROOT / path).read_text()) if path and (ROOT / path).is_file() else None


def source(candidate: str) -> dict:
    spec = importlib.util.spec_from_file_location("record_evidence",
                                                  ROOT / "scripts" / "record_evidence.py")
    rec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rec)
    sys.path.insert(0, str(ROOT))
    from app.llm.prompt_fingerprint import prompt_fingerprint
    return {"commit": candidate, "tree": git("rev-parse", f"{candidate}^{{tree}}"),
            "committed_at": git("show", "-s", "--format=%cI", candidate),
            # Read from the checkout: the candidate's own values when no build
            # input changed after it (after_candidate.build_inputs_changed).
            "versions": rec._versions(), "prompt_fingerprint": prompt_fingerprint(),
            "locks": {p: blob_sha256(candidate, p) for p in LOCKS}}


def blob_sha256(commit: str, path: str) -> str | None:
    """SHA-256 of a file as committed, or None when the commit has no such file."""
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{path}"], capture_output=True)
    return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else None


def delta(candidate: str, head: str) -> dict:
    paths = git("diff", "--name-only", candidate, head).splitlines() if candidate != head else []
    inputs = [p for p in paths if not p.startswith(NOT_INPUTS)]
    return {"head": head, "head_tree": git("rev-parse", f"{head}^{{tree}}"),
            "commits_after_candidate": int(git("rev-list", "--count", f"{candidate}..{head}")),
            "paths_changed": len(paths), "build_inputs_changed": inputs,
            "rule": f"a path is a build input unless it starts with one of {list(NOT_INPUTS)}"}


def records(pattern: str, candidate: str,
            measurements: str | None = None) -> tuple[list[dict], list[str]]:
    """Every record must have measured the candidate from a clean tree. A gate
    must also have passed; a measurement (a held-out evaluation set) records
    its outcome without one."""
    measured = {str(pathlib.Path(m)) for m in glob.glob(str(ROOT / measurements))} \
        if measurements else set()
    out, problems = [], []
    for path in sorted(glob.glob(str(ROOT / pattern))):
        if path.endswith((".detail.json",)):
            continue
        p = pathlib.Path(path)
        rec = json.loads(p.read_text())
        if "outcome" not in rec:
            continue
        app = rec.get("application") or {}
        entry = {"path": str(p.relative_to(ROOT)), "sha256": sha256(p),
                 "role": "measurement" if str(p) in measured else "gate",
                 "measured": app.get("sha"), "worktree_clean": app.get("worktree_clean"),
                 "status": rec["outcome"].get("status"), "counts": rec["outcome"].get("counts"),
                 "claim": (rec.get("claim") or "")[:160]}
        out.append(entry)
        if entry["measured"] != candidate:
            problems.append(f"{entry['path']} measured {entry['measured']}, not the candidate")
        if not entry["worktree_clean"]:
            problems.append(f"{entry['path']} measured a dirty tree")
        if entry["status"] != "passed" and entry["role"] == "gate":
            problems.append(f"{entry['path']}: {entry['status']}")
    return out, problems


def image(record: dict | None) -> dict:
    img = (record or {}).get("image") or {}
    return {
        "ref": img.get("ref"), "platform": img.get("platform"),
        "revision_label": img.get("revision_label"),
        "config_id": img.get("id"),
        "config_id_is": "the local image ID: the SHA-256 of the image's configuration JSON, "
                        "as the local engine reports it",
        "manifest_digest": img.get("digest"),
        "manifest_digest_is": "the digest of the image manifest in the local engine's store. "
                              "Not a registry digest: nothing was pushed, and a registry "
                              "computes its own when an image is pushed",
        "oci_index_digest": None,
        "oci_index_digest_is": "none: a single-platform local build has no index",
        "registry_digest": None, "published": False,
        "smoke_record": (record or {}).get("outcome", {}).get("status"),
    }


def scanner(gate: dict | None, full: dict | None, db: dict | None) -> dict:
    def vulns(report):
        return [v for t in (report or {}).get("Results", []) for v in (t.get("Vulnerabilities") or [])]
    counts: dict[str, dict[str, int]] = {}
    for v in vulns(full):
        sev = counts.setdefault(v["Severity"], {"fixable": 0, "unfixed": 0})
        sev["fixable" if v.get("FixedVersion") else "unfixed"] += 1
    vdb = (db or {}).get("VulnerabilityDB") or {}
    return {
        "tool": "trivy", "version": (db or {}).get("Version"),
        "database": {k: vdb.get(k) for k in ("Version", "UpdatedAt", "NextUpdate", "DownloadedAt")},
        "gate": {"policy": "no HIGH or CRITICAL vulnerability with a fixed version available",
                 "findings": len(vulns(gate)) if gate is not None else None,
                 "passed": (len(vulns(gate)) == 0) if gate is not None else None},
        "all_findings_by_severity": counts if full is not None else None,
        "scanned_at": (full or gate or {}).get("CreatedAt"),
        "image_id_scanned": ((full or gate or {}).get("Metadata") or {}).get("ImageID"),
    }


def exceptions() -> dict:
    gl = ROOT / ".gitleaksignore"
    tv = ROOT / ".trivyignore"
    return {"gitleaks": [line for line in gl.read_text().splitlines()
                         if line.strip() and not line.startswith("#")] if gl.exists() else [],
            "gitleaks_reason": "exact fingerprints of public example values, reviewed in "
                               "docs/SUPPLY_CHAIN.md",
            "trivy": tv.read_text().split() if tv.exists() else [],
            "pip_audit": [], "npm_audit": []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--records", required=True, help="glob of the candidate's command records")
    ap.add_argument("--measurements", help="glob of those records that are measurements, "
                                           "not gates (held-out evaluation sets)")
    ap.add_argument("--image-record")
    ap.add_argument("--trivy")
    ap.add_argument("--trivy-full")
    ap.add_argument("--trivy-db")
    ap.add_argument("--bundle", help="a JSON file describing the bundle (name, size, sha256)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    candidate = git("rev-parse", f"{args.candidate}^{{commit}}")
    recs, problems = records(args.records, candidate, args.measurements)
    external = load("evidence/external.json") or {}
    manifest = {
        "kind": "release-manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "source": source(candidate),
        "after_candidate": (after := delta(candidate, git("rev-parse", "HEAD"))),
        "records": recs,
        "image": image(load(args.image_record)),
        "scanner": scanner(load(args.trivy), load(args.trivy_full), load(args.trivy_db)),
        "exceptions": exceptions(),
        "hosted_ci": {"last_hosted_runs": external.get("hosted_ci", [])[-1:],
                      "candidate": "pending: not pushed (authorization required)"},
        "bundle": load(args.bundle),
        "problems": problems,
    }
    # Versions and the prompt fingerprint are read from the checkout.
    manifest["source"]["versions_valid_for_candidate"] = not after["build_inputs_changed"]
    if after["build_inputs_changed"]:
        problems.append("build inputs changed after the candidate: its records do not cover HEAD")
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1, sort_keys=False) + "\n")
    print(f"manifest -> {args.out}: {len(recs)} records, {len(problems)} problems")
    for p in problems:
        print("  " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
