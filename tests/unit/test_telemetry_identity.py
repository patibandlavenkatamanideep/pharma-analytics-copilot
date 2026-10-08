"""Two processes must export as two series, not one.

A cumulative counter is exported as the running total of ONE process. The
collector and Prometheus key a series by metric, labels and the resource's
identity; two processes with the same resource write the same series, so
each export overwrites the other's total, and rates over it are wrong. The
image runs `uvicorn --workers 2`, so this is every deployment, not only a
scaled one (qualification of 7 October 2026, step 6).

Each process exports one counter increment through the real redacting
exporter, with only the network call replaced by a capture.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

CHILD = r'''
import json
from opentelemetry.exporter.otlp.proto.http import metric_exporter as otlp
from opentelemetry.sdk.metrics.export import MetricExportResult

captured = []

class Capture(otlp.OTLPMetricExporter):
    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        for rm in metrics_data.resource_metrics:
            captured.append(dict(rm.resource.attributes))
        return MetricExportResult.SUCCESS

otlp.OTLPMetricExporter = Capture
from app import telemetry

class Settings:
    otel_endpoint = "http://127.0.0.1:9"
    otel_timeout_s = 1
    release = "identity-test"

telemetry.configure(Settings(), freshness_reader=lambda: None)
telemetry.count("pac.persistence.failures", kind="audit")
telemetry._state.providers[1].force_flush()
print("RESOURCES " + json.dumps(captured))
'''


def exported_resource() -> dict:
    r = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}, timeout=60)
    line = next((x for x in r.stdout.splitlines() if x.startswith("RESOURCES ")), None)
    assert line, r.stdout[-1000:] + r.stderr[-2000:]
    resources = json.loads(line[len("RESOURCES "):])
    assert resources, "nothing was exported"
    return resources[0]


def test_two_processes_export_under_distinct_opaque_identities():
    first, second = exported_resource(), exported_resource()
    assert first.get("service.instance.id") and second.get("service.instance.id")
    assert first["service.instance.id"] != second["service.instance.id"]
    # Opaque: not a host name, a pid or anything else about where it runs.
    assert re.fullmatch(r"[0-9a-f]{32}", first["service.instance.id"])
    assert set(first) == set(second)
