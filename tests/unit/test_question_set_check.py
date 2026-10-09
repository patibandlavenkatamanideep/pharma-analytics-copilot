"""scripts/check_question_set.py refuses what would make a fresh set measure
less than it claims: schema errors, reused ids, and questions copied from the
sets the system was developed on."""

from __future__ import annotations

import importlib.util
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_question_set", ROOT / "scripts" / "check_question_set.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def write(tmp_path, questions, status="holdout"):
    path = tmp_path / "set.yaml"
    path.write_text(yaml.safe_dump({"version": "1", "status": status, "questions": questions}))
    return path


GOOD = {"id": "fresh-01", "family": "volume", "principal": "exec",
        "question": "How many packs of Paxelium were sold in the Mountain region last quarter?",
        "expect": {"type": "sql", "compare": "scalar", "sql": "SELECT 1"}}


def test_a_well_formed_fresh_set_passes(tmp_path):
    errors, _ = checker.check(write(tmp_path, [GOOD]))
    assert errors == []


def test_a_question_copied_from_a_development_set_is_an_error(tmp_path):
    dev = yaml.safe_load((ROOT / "evals" / "holdout2.yaml").read_text())["questions"][0]
    copied = {**GOOD, "id": "fresh-02", "question": dev["question"]}
    errors, _ = checker.check(write(tmp_path, [copied]))
    assert any("identical to" in e for e in errors)


def test_a_reused_id_and_schema_errors_are_reported(tmp_path):
    reused = {**GOOD, "id": "k-07"}
    broken = {"id": "fresh-03", "family": "x", "principal": "nobody",
              "question": "Total volume?", "expect": {"type": "sql"}}
    errors, _ = checker.check(write(tmp_path, [reused, broken, GOOD, dict(GOOD)]))
    joined = "\n".join(errors)
    assert "already used" in joined and "principal" in joined
    assert "needs reference SQL" in joined and "duplicated id" in joined


def test_a_paraphrase_of_a_development_question_is_a_warning(tmp_path):
    near = {**GOOD, "id": "fresh-04",
            "question": "Is Zenovax volume growing or declining month over month now?"}
    errors, warnings = checker.check(write(tmp_path, [near]))
    assert errors == [] and any("near-duplicate" in w for w in warnings)
