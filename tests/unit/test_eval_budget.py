"""A live evaluation spends real money, so it is bounded before it starts;
and a holdout is only a holdout if it was frozen before anyone saw answers.

No model is called: the budget is exercised with recorded usage shapes, and
the refusals happen before any database or provider is touched.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("run_evals", ROOT / "scripts" / "run_evals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attempt(i, o, known=True):
    return {"usage": {"input_tokens": i, "output_tokens": o, "known": known}}


def test_reported_usage_is_charged_as_reported(ev):
    b = ev.Budget(100_000, 10_000)
    b.charge({"attempts": [attempt(4_670, 160)]})
    b.charge({"attempts": [attempt(4_500, 300), attempt(4_900, 150)]})   # a repair
    assert (b.input, b.output, b.unreported_calls) == (14_070, 610, 0)


def test_an_unreported_call_is_charged_at_the_ceiling_never_zero(ev):
    b = ev.Budget(100_000, 100_000)
    b.charge({"attempts": [attempt(None, None, known=False)]})
    assert (b.input, b.output, b.unreported_calls) == (8_000, 4_096, 1)
    b.charge(None)                               # planning failed before reporting
    assert (b.input, b.output, b.unreported_calls) == (24_000, 12_288, 3)


def test_the_next_question_runs_only_if_its_worst_case_still_fits(ev):
    b = ev.Budget(20_000, 10_000)
    assert b.can_afford_another()                # 2 x 8,000 = 16,000 <= 20,000
    b.charge({"attempts": [attempt(4_670, 160)]})
    assert not b.can_afford_another()            # 4,670 + 16,000 > 20,000


def test_the_smoke_subset_is_one_question_per_family(ev):
    qs = [{"id": "a1", "family": "a"}, {"id": "a2", "family": "a"},
          {"id": "b1", "family": "b"}, {"id": "c1"}]
    assert [q["id"] for q in ev.smoke_subset(qs)] == ["a1", "b1", "c1"]


@pytest.mark.parametrize("name", ["questions.yaml", "holdout.yaml", "holdout2.yaml"])
def test_the_existing_sets_say_what_they_are(ev, name):
    import yaml
    path = ROOT / "evals" / name
    ok, status = ev.runnable(path, yaml.safe_load(path.read_text()))
    assert ok and status in ("regression", "spent")


def test_a_holdout_runs_only_as_frozen(ev, tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "FROZEN", tmp_path / "frozen.json")
    holdout = tmp_path / "holdout9.yaml"
    holdout.write_text('status: "holdout"\nversion: "1"\nquestions: []\n')
    spec = {"status": "holdout"}
    ok, why = ev.runnable(holdout, spec)
    assert not ok and "not been frozen" in why

    (tmp_path / "frozen.json").write_text(json.dumps({"holdout9.yaml": {
        "sha256": ev.file_sha256(holdout), "frozen_at": "2026-10-01T00:00:00+00:00"}}))
    assert ev.runnable(holdout, spec) == (True, "holdout")

    holdout.write_text('status: "holdout"\nversion: "1"\nquestions: [{id: changed}]\n')
    ok, why = ev.runnable(holdout, spec)
    assert not ok and "changed after it was frozen" in why


def test_a_set_with_no_status_is_refused(ev, tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("questions: []\n")
    assert ev.runnable(path, {})[0] is False


def test_a_live_run_without_a_budget_is_refused_before_anything_starts(ev, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_evals.py", "--provider", "bedrock"])
    assert ev.main() == 2
    assert "needs a budget" in capsys.readouterr().err
