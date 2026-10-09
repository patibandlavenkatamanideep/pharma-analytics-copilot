"""What the collector configuration writes, and where.

deploy/observability/otel-collector.yaml is the configuration a deployment
starts from. Its own header says traces and metrics are written to local
files only when PAC_OTEL_FILE_DIR is set; a file exporter in a pipeline
writes whether or not it is set (to /tmp by default), growing without bound
on the collector's host.

The drill scans those files for sentinels after the collector has been
killed and restarted. A file exporter that does not append truncates its
file at start, so the scan then covers only what was exported after the
restart (evidence/probes/collector_restart_keeps_exports.py runs the real
collector to show it). The files are an overlay the drill adds,
otel-collector.local-files.yaml, and they append.
"""

from __future__ import annotations

import importlib.util
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
OBS = ROOT / "deploy" / "observability"


def merged(paths: list[pathlib.Path]) -> dict:
    """The collector's merge of several --config files: maps merge key by
    key, anything else (lists included) is replaced by the later file."""
    def merge(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            return {k: merge(a[k], b[k]) if k in a and k in b else b.get(k, a.get(k))
                    for k in {**a, **b}}
        return b
    out: dict = {}
    for p in paths:
        out = merge(out, yaml.safe_load(pathlib.Path(p).read_text()))
    return out


def exporters_in_use(config: dict) -> set[str]:
    return {e for p in config["service"]["pipelines"].values() for e in p.get("exporters", [])}


def drill_configs() -> list[pathlib.Path]:
    spec = importlib.util.spec_from_file_location("ops_drill", ROOT / "scripts" / "ops_drill.py")
    drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drill)
    return list(getattr(drill, "COLLECTOR_CONFIGS", [OBS / "otel-collector.yaml"]))


def test_the_deployable_collector_writes_no_local_files():
    config = yaml.safe_load((OBS / "otel-collector.yaml").read_text())
    assert not {e for e in exporters_in_use(config) if e.split("/")[0] == "file"}


def test_the_drill_keeps_every_export_across_collector_restarts():
    config = merged(drill_configs())
    files = {e for e in exporters_in_use(config) if e.split("/")[0] == "file"}
    assert files, "the drill scans files the collector does not write"
    for name in files:
        assert config["exporters"][name].get("append") is True, name
