#!/usr/bin/env python3
"""Check a question set against the packet's schema, and against the sets the
system was developed on.

    python3 scripts/check_question_set.py evals/<set>.yaml

Fails (exit 1) on a schema error, a duplicated id, or an id or question that
already appears in a development set. Reports near-duplicates of development
questions (most of their words shared) as warnings: a holdout that paraphrases
a development question measures less than it appears to. This checks text,
not the author's exposure, which only evals/packet/CONTAMINATION_LOG.md
records.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEVELOPMENT = ["evals/questions.yaml", "evals/holdout.yaml", "evals/holdout2.yaml"]


def questions(spec: dict) -> list[tuple[str, str]]:
    out = []
    for item in spec.get("questions", []):
        texts = [item["question"]] if "question" in item else [t.get("question", "") for t in item.get("turns", [])]
        out += [(item.get("id", "?"), t) for t in texts]
    return out


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def check(path: pathlib.Path) -> tuple[list[str], list[str]]:
    schema = json.loads((ROOT / "evals" / "packet" / "oracle_schema.json").read_text())
    props, required = schema["properties"], schema["required"]
    types = set(props["expect"]["properties"]["type"]["enum"])
    principals = set(props["principal"]["enum"])
    spec = yaml.safe_load(path.read_text())
    errors, warnings = [], []
    if spec.get("status") not in ("holdout", "development", "regression", "spent"):
        errors.append(f"status {spec.get('status')!r} is not holdout, development, regression or spent")
    seen = set()
    for item in spec.get("questions", []):
        qid = item.get("id", "?")
        if qid in seen:
            errors.append(f"{qid}: duplicated id")
        seen.add(qid)
        if extra := set(item) - set(props):
            errors.append(f"{qid}: unexpected fields {sorted(extra)}")
        if missing := [k for k in required if k not in item]:
            errors.append(f"{qid}: missing {missing}")
        if ("question" in item) == ("turns" in item):
            errors.append(f"{qid}: exactly one of question or turns")
        if item.get("principal") not in principals:
            errors.append(f"{qid}: principal {item.get('principal')!r} not in {sorted(principals)}")
        if "question" in item and "expect" not in item:
            errors.append(f"{qid}: a single question needs expect")
        expects = ([item["expect"]] if "expect" in item else []) + \
            [t.get("expect") or {} for t in item.get("turns", [])]
        for e in expects:
            if e.get("type") not in types:
                errors.append(f"{qid}: expect.type {e.get('type')!r} unknown")
            if e.get("type") == "sql" and not e.get("sql"):
                errors.append(f"{qid}: a sql expectation needs reference SQL")
    dev = {}
    for name in DEVELOPMENT:
        if (ROOT / name).resolve() == path.resolve():
            continue
        for qid, text in questions(yaml.safe_load((ROOT / name).read_text())):
            dev[(name, qid)] = text
    dev_ids = {qid for _, qid in dev}
    for qid, text in questions(spec):
        if qid in dev_ids:
            errors.append(f"{qid}: id already used by a development set")
        for (name, other_id), other in dev.items():
            a, b = words(text), words(other)
            if text.strip().lower() == other.strip().lower():
                errors.append(f"{qid}: identical to {name} {other_id}")
            elif a and b and len(a & b) / len(a | b) >= 0.7:
                warnings.append(f"{qid}: near-duplicate of {name} {other_id}")
    return errors, warnings


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    errors, warnings = check(pathlib.Path(sys.argv[1]))
    for w in warnings:
        print("WARNING  " + w)
    for e in errors:
        print("ERROR    " + e)
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
