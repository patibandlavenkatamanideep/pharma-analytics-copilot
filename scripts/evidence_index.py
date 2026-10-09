#!/usr/bin/env python3
"""Build the current evidence index from machine-readable results.

    python3 scripts/evidence_index.py --candidate <sha> [--junit <full-suite.junit.xml>]
                                      [--records 'evidence/runs/r5-final-*.json'] [--check]

Writes evidence/index.json and docs/EVIDENCE_INDEX.md. Nothing in either is typed by
hand: identities come from git, versions from the code, results from evidence records,
test categories from a JUnit file and evidence/test_categories.json, evaluation
outcomes from the evaluator's detail files, defects from evidence/ledger.json and
hosted runs from evidence/external.json. Every reference is checked; whatever does
not resolve is listed under "problems" or "missing", never silently dropped.

Two SHAs are kept apart: the executable CANDIDATE that the records measured, and the
current HEAD. Every path changed between them is classified, so a documentation-only
delta can be told from one that needs new evidence.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import pathlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIRED = ("evidence_schema_version", "recorded_at", "application", "environment", "command", "outcome")
STATUSES = {"passed", "failed", "blocked", "not_run", "not_applicable"}
#: Exception types that mean "the baseline lacks an interface the test calls",
#: as opposed to "the baseline behaves wrongly". Approximate, and labelled so.
INTERFACE = ("collection failure", "ImportError", "ModuleNotFoundError", "AttributeError")

#: Path classes for the candidate..HEAD delta, first match wins.
PATH_CLASSES = [
    ("evidence data", ["evidence/runs/*", "evidence/index.json", "evidence/ledger.json",
                       "evidence/test_categories.json", "evidence/external.json"]),
    ("probe tooling", ["evidence/probes/*"]),
    ("documentation", ["docs/*", "*.md"]),
    ("test code", ["tests/*"]),
    ("evaluation sets", ["evals/*"]),
    ("workflow", [".github/*"]),
    ("deployment configuration", ["compose.yaml", "Dockerfile", "deploy/*", "infra/*", "terraform/*"]),
    ("application and build", ["app/*", "scripts/*", "migrations/*", "schema/*", "web/*",
                               "requirements*", "pyproject.toml", "package*.json", "*"]),
]
NEEDS_EVIDENCE = {"test code", "evaluation sets", "workflow", "deployment configuration",
                  "application and build"}


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True).returncode == 0


def classify(path: str) -> str:
    for name, patterns in PATH_CLASSES:
        if any(fnmatch.fnmatch(path, p) for p in patterns):
            return name
    return "application and build"


def versions() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from app.analytics.registry import get_registry
    from app.data.classification import RULE_VERSION
    from app.data.manifest import MAPPING_VERSION
    from app.data.schema_contract import CONTRACT_VERSION
    from app.graph.turn import GRAPH_VERSION
    from app.llm.planner import PLANNER_CONTRACT_VERSION, PROMPT_VERSION
    from app.llm.prompt_fingerprint import prompt_fingerprint
    from app.pipeline import POLICY_VERSION
    return {"metric_registry": get_registry().version, "policy": POLICY_VERSION,
            "schema_contract": CONTRACT_VERSION, "mapping": MAPPING_VERSION,
            "classification_rule": RULE_VERSION, "prompt": PROMPT_VERSION,
            "prompt_fingerprint": prompt_fingerprint(), "planner_contract": PLANNER_CONTRACT_VERSION,
            "graph": GRAPH_VERSION}


def load_record(path: pathlib.Path) -> tuple[dict | None, list[str]]:
    problems = []
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, [f"{path.name}: does not parse ({type(exc).__name__})"]
    missing = [k for k in REQUIRED if k not in record]
    if missing:
        problems.append(f"{path.name}: missing required keys {missing}")
    status = (record.get("outcome") or {}).get("status")
    if status not in STATUSES:
        problems.append(f"{path.name}: outcome.status {status!r} is not one of {sorted(STATUSES)}")
    return record, problems


def junit_cases(path: pathlib.Path) -> list[dict[str, str]]:
    cases = []
    for case in ET.parse(path).getroot().iter("testcase"):
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                outcome, message = tag, el.get("message") or ""
                break
        classname = case.get("classname") or ""
        file = classname.replace(".", "/") + ".py" if classname else ""
        cases.append({"file": file, "name": case.get("name") or "", "outcome": outcome,
                      "type": message.split(":")[0].strip()[:60] if message else ""})
    return cases


def categories(cases: list[dict], mapping: dict) -> dict[str, Any]:
    owner = {f: cat for cat, spec in mapping["categories"].items() for f in spec["files"]}
    duplicates = [f for f, n in Counter(f for spec in mapping["categories"].values()
                                        for f in spec["files"]).items() if n > 1]
    per = defaultdict(Counter)
    uncategorized = set()
    for case in cases:
        cat = owner.get(case["file"])
        if cat is None:
            uncategorized.add(case["file"])
            cat = "UNCATEGORIZED"
        per[cat][case["outcome"]] += 1
    seen = {c["file"] for c in cases}
    return {"by_category": {cat: {"tests": sum(c.values()), **dict(c),
                                  "meaning": mapping["categories"].get(cat, {}).get("meaning", "")}
                            for cat, c in sorted(per.items())},
            "uncategorized_files": sorted(uncategorized),
            "mapped_but_not_collected": sorted(set(owner) - seen),
            "files_in_two_categories": duplicates}


def evaluation(detail_path: pathlib.Path, record: dict | None) -> dict[str, Any]:
    d = json.loads(detail_path.read_text())
    failures = [{"id": r["id"], "category": r.get("category"), "reason": (r.get("reason") or "")[:200]}
                for r in d["results"] if not r.get("passed")]
    out = {"set": detail_path.name, "question_set_status": d.get("question_set_status"),
           "question_set_sha256": d.get("question_set_sha256"), "provider": d.get("provider"),
           "measures_nl_accuracy": d.get("measures_nl_accuracy"), "prompt_version": d.get("prompt_version"),
           "dataset_id": d.get("dataset_id"), "total": d.get("total"), "passed": d.get("passed"),
           "failed": d.get("failed"), "failures": failures,
           "by_category": d.get("by_category")}
    out["detail_consistent"] = (len(d["results"]) == d.get("total")
                                and sum(1 for r in d["results"] if r.get("passed")) == d.get("passed"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--records", default="evidence/runs/r5-final-*.json")
    ap.add_argument("--junit", default=None)
    ap.add_argument("--check", action="store_true", help="exit 1 if anything is unresolved")
    args = ap.parse_args()

    problems: list[str] = []
    missing: list[str] = []
    head, candidate = git("rev-parse", "HEAD"), git("rev-parse", args.candidate + "^{commit}")
    identity = {
        "head": {"sha": head, "tree": git("rev-parse", "HEAD^{tree}"),
                 "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                 "worktree_clean": git("status", "--porcelain") == ""},
        "candidate": {"sha": candidate, "tree": git("rev-parse", candidate + "^{tree}"),
                      "is_ancestor_of_head": git_ok("merge-base", "--is-ancestor", candidate, head)},
    }
    if not identity["candidate"]["is_ancestor_of_head"]:
        problems.append("the candidate is not an ancestor of HEAD")
    delta = defaultdict(list)
    for path in git("diff", "--name-only", candidate, head).splitlines():
        delta[classify(path)].append(path)
    identity["delta_candidate_to_head"] = {
        "by_class": {k: sorted(v) for k, v in sorted(delta.items())},
        "classes_needing_evidence": sorted(set(delta) & NEEDS_EVIDENCE),
    }

    # -- records ---------------------------------------------------------------------
    records, images, datasets = [], [], {}
    for path in sorted(pathlib.Path(p) for p in glob.glob(str(ROOT / args.records))):
        if path.name.endswith(".detail.json"):
            continue
        record, issues = load_record(path)
        problems += issues
        if record is None:
            continue
        app = record.get("application") or {}
        entry = {"file": f"evidence/runs/{path.name}", "claim": record.get("claim"),
                 "status": record["outcome"].get("status"), "counts": record["outcome"].get("counts"),
                 "summary": record["outcome"].get("summary"), "sha": app.get("sha"),
                 "worktree_clean": app.get("worktree_clean"), "limits": record.get("limits"),
                 "recorded_at": record.get("recorded_at")}
        entry["measures_candidate"] = app.get("sha") == candidate and app.get("worktree_clean") is True
        if not entry["measures_candidate"]:
            problems.append(f"{path.name}: measured {str(app.get('sha'))[:12]} "
                            f"(clean={app.get('worktree_clean')}), not the candidate")
        records.append(entry)
        if record.get("image"):
            images.append({"file": path.name, **record["image"],
                           "note": "id is the local image configuration ID; digest is the local "
                                   "manifest digest reported by the container engine. Neither is a "
                                   "published registry digest."})
        ds = record.get("dataset") or {}
        if ds.get("dataset_id"):
            datasets[ds["dataset_id"]] = {k: ds.get(k) for k in ("dataset_id", "load_mode",
                                                                 "fingerprint", "database")}

    # -- tests by category -------------------------------------------------------------
    mapping = json.loads((ROOT / "evidence/test_categories.json").read_text())
    tests: dict[str, Any] = {"junit": args.junit}
    if args.junit and (ROOT / args.junit).exists():
        cases = junit_cases(ROOT / args.junit)
        tests.update(categories(cases, mapping))
        totals = Counter(c["outcome"] for c in cases)
        tests["totals"] = {"tests": len(cases), **dict(totals)}
        for issue in ("uncategorized_files", "mapped_but_not_collected", "files_in_two_categories"):
            if tests[issue]:
                problems.append(f"test categories: {issue.replace('_', ' ')}: {tests[issue]}")
        full = next((r for r in records if "pytest" in r["file"] and r["counts"]), None)
        if full:
            tests["record_counts"] = full["counts"]
            rc = full["counts"]
            tests["junit_matches_record"] = (
                rc.get("passed", 0) == totals.get("passed", 0)
                and rc.get("failed", 0) == totals.get("failure", 0)
                and rc.get("error", 0) == totals.get("error", 0)
                and rc.get("skipped", 0) == totals.get("skipped", 0))
            if not tests["junit_matches_record"]:
                problems.append(f"JUnit {dict(totals)} disagrees with {full['file']} {rc}")
    else:
        missing.append(f"full-suite JUnit for the candidate ({args.junit or 'not given'})")

    # -- evaluations -------------------------------------------------------------------
    evaluations = []
    for detail in sorted(glob.glob(str(ROOT / args.records.replace("*.json", "eval-*.detail.json")))):
        rec_path = pathlib.Path(detail.replace(".detail.json", ".json"))
        rec = json.loads(rec_path.read_text()) if rec_path.exists() else None
        ev = evaluation(pathlib.Path(detail), rec)
        if not ev["detail_consistent"]:
            problems.append(f"{ev['set']}: detail totals disagree with its results")
        evaluations.append(ev)

    # -- defect ledger -----------------------------------------------------------------
    ledger = json.loads((ROOT / "evidence/ledger.json").read_text())
    collected = Counter(c["file"] for c in junit_cases(ROOT / args.junit)) if tests.get("totals") else Counter()
    failed_files = {c["file"] for c in junit_cases(ROOT / args.junit) if c["outcome"] in ("failure", "error")} \
        if tests.get("totals") else set()
    defects = []
    for d in ledger["defects"]:
        item = {"id": d["id"], "title": d["title"], "fix_commits": [], "regression": [], "reproductions": []}
        item["verified_by"] = d.get("verified_by")
        for c in d["fix_commits"]:
            exists = git_ok("cat-file", "-e", c + "^{commit}")
            ancestor = exists and git_ok("merge-base", "--is-ancestor", c, candidate)
            in_head = exists and git_ok("merge-base", "--is-ancestor", c, head)
            item["fix_commits"].append({"commit": c, "exists": exists, "in_candidate": ancestor,
                                        "in_head": in_head})
            if not in_head:
                problems.append(f"{d['id']}: fix commit {c} is not in HEAD")
            elif not ancestor and not d.get("verified_by"):
                problems.append(f"{d['id']}: fix commit {c} is after the candidate and nothing verifies it")
        for f in d["regression_files"]:
            entry = {"file": f, "exists": (ROOT / f).exists(), "collected": collected.get(f, 0),
                     "all_passed_on_candidate": f not in failed_files and collected.get(f, 0) > 0}
            item["regression"].append(entry)
            if not entry["exists"]:
                problems.append(f"{d['id']}: regression file {f} is missing")
        for r in d["reproductions"]:
            art = r["artifact"]
            rep = {"artifact": art, "baseline": r.get("baseline"), "kind": r.get("kind", "record")}
            if rep["kind"] == "hosted-ci-run":
                rep["status"] = "see evidence/external.json"
            elif not (ROOT / art).exists():
                rep["status"] = "MISSING"
                missing.append(f"{d['id']}: {art}")
            elif rep["kind"] == "summary":
                rep["status"] = "summary only (hand-assembled; not a command record)"
            else:
                rec, issues = load_record(ROOT / art)
                problems += issues
                if rec and rep["kind"] == "probe":
                    # Recorded from the checkout that holds the probe; the probe names the
                    # code it actually ran in its result file.
                    result = json.loads((ROOT / r["result"]).read_text()) if (ROOT / r.get("result", "")).is_file() else {}
                    rep.update(status=rec["outcome"].get("status"), summary=rec["outcome"].get("summary"),
                               measured=str(result.get("baseline", ""))[:12], result=r.get("result"))
                    if not str(result.get("baseline", "")).startswith(r.get("baseline") or "?"):
                        problems.append(f"{d['id']}: probe result does not name baseline {r.get('baseline')}")
                    if rec["outcome"].get("status") != "failed":
                        problems.append(f"{d['id']}: reproduction {art} did not fail")
                elif rec:
                    app = rec.get("application") or {}
                    rep.update(status=rec["outcome"].get("status"), counts=rec["outcome"].get("counts"),
                               summary=rec["outcome"].get("summary"), measured=str(app.get("sha"))[:12],
                               worktree_clean=app.get("worktree_clean"),
                               dirty_files=len(app.get("dirty_digests") or {}))
                    if r.get("baseline") and not str(app.get("sha", "")).startswith(r["baseline"]):
                        problems.append(f"{d['id']}: {art} measured {app.get('sha')} not {r['baseline']}")
                    if rec["outcome"].get("status") != "failed":
                        problems.append(f"{d['id']}: reproduction {art} did not fail")
                if r.get("junit") and (ROOT / r["junit"]).exists():
                    kinds = Counter()
                    for case in junit_cases(ROOT / r["junit"]):
                        if case["outcome"] in ("failure", "error"):
                            kinds["interface" if case["type"].startswith(INTERFACE) else "behavioural"] += 1
                    rep["failures_by_kind"] = dict(kinds)
                if r.get("note"):
                    rep["note"] = r["note"]
            item["reproductions"].append(rep)
        defects.append(item)
    summary_doc = ROOT / "evidence/runs/r5-original-reproductions.json"
    if summary_doc.exists():
        for name in json.loads(summary_doc.read_text()).get("original_logs", {}):
            if not list(ROOT.glob(f"**/{name}")):
                missing.append(f"r5-original-reproductions.json hashes {name}, which is not preserved")

    external = json.loads((ROOT / "evidence/external.json").read_text()) \
        if (ROOT / "evidence/external.json").exists() else {}
    for run in external.get("hosted_ci", []):
        if not git_ok("cat-file", "-e", run["commit"] + "^{commit}"):
            problems.append(f"hosted run {run['run_id']}: commit {run['commit']} does not exist here")

    index = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "generator": "scripts/evidence_index.py", "identity": identity, "versions": versions(),
             "datasets": list(datasets.values()), "images": images, "records": records,
             "tests": tests, "evaluations": evaluations, "defects": defects,
             "external": external, "missing": missing, "problems": problems}
    (ROOT / "evidence/index.json").write_text(json.dumps(index, indent=1) + "\n")
    (ROOT / "docs/EVIDENCE_INDEX.md").write_text(render(index))
    print(f"evidence index: {len(records)} records, {len(defects)} defects, "
          f"{len(problems)} problems, {len(missing)} missing")
    return 1 if args.check and (problems or missing) else 0


def render(ix: dict[str, Any]) -> str:
    i, c = ix["identity"], ix["identity"]["candidate"]
    out = ["# Evidence index", "",
           "Generated by `scripts/evidence_index.py` from `evidence/index.json`. Do not edit by hand:",
           "regenerate it. Every figure below comes from a record, a JUnit file, an evaluator",
           "detail file, git or the code.", "",
           "## Identity", "",
           "| | SHA | Tree |", "|---|---|---|",
           f"| Executable candidate (what the records measured) | `{c['sha']}` | `{c['tree']}` |",
           f"| HEAD (`{i['head']['branch']}`, clean: {i['head']['worktree_clean']}) | `{i['head']['sha']}` | `{i['head']['tree']}` |",
           "", "Changed between the candidate and HEAD:", ""]
    for cls, paths in i["delta_candidate_to_head"]["by_class"].items():
        out.append(f"- **{cls}** ({len(paths)}): " + ", ".join(f"`{p}`" for p in paths[:8])
                   + (" …" if len(paths) > 8 else ""))
    need = i["delta_candidate_to_head"]["classes_needing_evidence"]
    out += ["", ("**Needs evidence beyond the candidate's records:** " + ", ".join(need) + ". "
                 "See the external runs below for what covered them.") if need else
            "The delta is documentation and evidence only: the candidate's records describe HEAD's executable bytes.", ""]
    out += ["## Versions", "", "| Contract | Version |", "|---|---|"]
    out += [f"| {k} | `{v}` |" for k, v in ix["versions"].items()]
    out += ["", "## Records for the candidate", "",
            "| Record | Status | Result | Measures the candidate |", "|---|---|---|---|"]
    for r in ix["records"]:
        out.append(f"| `{r['file'].split('/')[-1]}` | {r['status']} | {str(r['summary'] or '')[:90].replace('|', '/')} | "
                   f"{'yes' if r['measures_candidate'] else 'NO'} |")
    t = ix["tests"]
    if t.get("by_category"):
        out += ["", "## What the test suite establishes, by category", "",
                f"From `{t['junit']}`: {t['totals']['tests']} tests, each counted once "
                "(the security and ingestion gates are subsets of this run, not added to it).", "",
                "| Category | Tests | Passed | Not passed | Establishes |", "|---|---:|---:|---:|---|"]
        for cat, v in t["by_category"].items():
            bad = v["tests"] - v.get("passed", 0)
            out.append(f"| {cat} | {v['tests']} | {v.get('passed', 0)} | {bad} | {v['meaning']} |")
    out += ["", "## Evaluations (offline planner unless stated)", "",
            "| Set | Status | Provider | Passed | Failed | Failures |", "|---|---|---|---:|---:|---|"]
    for e in ix["evaluations"]:
        fails = "; ".join(f"{f['id']} ({f['category']}): {f['reason'][:70]}" for f in e["failures"]) or "—"
        out.append(f"| `{e['set']}` | {e['question_set_status']} | {e['provider']} | {e['passed']} | {e['failed']} | {fails} |")
    out += ["", "Offline results exercise compilation, authorization, execution and rendering with a",
            "deterministic planner. They are not natural-language accuracy (`measures_nl_accuracy` is",
            "false in every detail file).", "", "## Defects", ""]
    for d in ix["defects"]:
        fixes = ", ".join(f"`{f['commit']}`" + ("" if f["in_candidate"] else
                          (f" (after the candidate; verified by {d['verified_by']})" if d.get("verified_by") else " (NOT IN CANDIDATE)"))
                          for f in d["fix_commits"])
        regs = ", ".join(f"`{r['file'].split('/')[-1]}` ({r['collected']}" +
                         (", all pass)" if r["all_passed_on_candidate"] else ", NOT ALL PASS)") for r in d["regression"]) or "none (CI configuration)"
        out += [f"### {d['id']}", "", d["title"] + ".", "", f"- Fix: {fixes}", f"- Regression on the candidate: {regs}"]
        for r in d["reproductions"]:
            extra = ""
            if r.get("failures_by_kind"):
                extra = " — failures by kind: " + ", ".join(f"{k} {v}" for k, v in r["failures_by_kind"].items())
            out.append(f"- Reproduction on `{r.get('baseline')}`: `{r['artifact'].split('/')[-1]}` — "
                       f"{r.get('status')}{' — ' + str(r.get('summary'))[:60] if r.get('summary') else ''}{extra}"
                       + (f" ({r['note']})" if r.get("note") else ""))
        out.append("")
    if ix["external"]:
        out += ["## Externally verified (hosted)", ""]
        for run in ix["external"].get("hosted_ci", []):
            out.append(f"- Run [{run['run_id']}]({run['url']}) on `{run['commit'][:7]}`: **{run['conclusion']}** — {run['note']}")
        out += [""]
    out += ["## Missing artifacts", ""] + ([f"- {m}" for m in ix["missing"]] or ["- none"])
    out += ["", "## Unresolved problems", ""] + ([f"- {p}" for p in ix["problems"]] or ["- none"])
    out += ["", "Interface failures (ImportError, AttributeError, collection failure) mean the baseline",
            "lacked something the test calls; behavioural failures are wrong results or crashes. The",
            "split is by exception type and approximate.", ""]
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
