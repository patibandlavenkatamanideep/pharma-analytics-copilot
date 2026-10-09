"""The alert rules, their documentation, the dashboard and the metrics agree.

deploy/observability/alerts.yml is what Prometheus evaluates; the table in
docs/OBSERVABILITY.md#alerts is what an operator reads; dashboard.json is
what they look at. A rule or panel naming a metric the application does not
export never fires and never draws -- silently. These checks need no
running backend; scripts/ops_drill.py runs the same files against one.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
OBS = ROOT / "deploy" / "observability"


def rules() -> dict[str, dict]:
    doc = yaml.safe_load((OBS / "alerts.yml").read_text())
    out: dict[str, dict] = {}
    for group in doc["groups"]:
        for rule in group["rules"]:
            assert rule["alert"] not in out, f"duplicate alert {rule['alert']}"
            out[rule["alert"]] = rule
    return out


def documented() -> dict[str, tuple[str, str]]:
    text = (ROOT / "docs" / "OBSERVABILITY.md").read_text()
    section = text.split("## Alerts", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        m = re.match(r"^\| `([A-Za-z]+)` \| `(.+?)` \| (.*?) \|", line)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3).strip())
    return rows


def exported_names() -> set[str]:
    """Prometheus names of the declared instruments, as the collector's
    Prometheus exporter writes them (checked against a live scrape by the
    drill): dots to underscores, the unit as a word, _total on counters,
    _bucket/_sum/_count on histograms; a {annotation} unit adds nothing."""
    from app.telemetry import INSTRUMENTS, OBSERVED

    unit_word = {"ms": "_milliseconds", "s": "_seconds"}
    names = set()
    for name, (kind, unit, _) in INSTRUMENTS.items():
        base = name.replace(".", "_") + unit_word.get(unit, "")
        if kind == "counter":
            names.add(base + "_total")
        elif kind == "histogram":
            names |= {base + "_bucket", base + "_sum", base + "_count"}
        else:
            names.add(base)
    for name, (unit, _, _) in OBSERVED.items():
        names.add(name.replace(".", "_") + unit_word.get(unit, ""))
    return names


def referenced(expr: str) -> set[str]:
    return set(re.findall(r"\bpac_[a-z_]+", expr))


def test_every_rule_is_documented_with_the_same_expression_and_duration():
    doc = documented()
    assert set(doc) == set(rules()), (sorted(set(rules()) - set(doc)), sorted(set(doc) - set(rules())))
    for name, rule in rules().items():
        expr, for_ = doc[name]
        assert expr == rule["expr"], name
        assert for_ == rule.get("for", "—"), name


def test_every_metric_a_rule_or_panel_names_is_exported():
    names = exported_names()
    exprs = [r["expr"] for r in rules().values()]
    dashboard = json.loads((OBS / "dashboard.json").read_text())
    exprs += [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    unknown = sorted({m for e in exprs for m in referenced(e)} - names)
    assert not unknown, unknown


def test_the_drill_shortens_every_rule_and_only_by_substitution():
    spec = importlib.util.spec_from_file_location("ops_drill", ROOT / "scripts" / "ops_drill.py")
    drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drill)
    assert set(drill.DRILL_RULES) == set(rules())
    for name, rule in rules().items():
        for old in drill.DRILL_RULES[name]["sub"]:
            assert old in rule["expr"], (name, old)
    assert set(drill.NOT_EXERCISED) <= set(rules())
