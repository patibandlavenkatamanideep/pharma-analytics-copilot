#!/usr/bin/env python3
"""Does the collector, as the drill configures it, keep what it wrote before a restart?

    python3 evidence/probes/collector_restart_keeps_exports.py --tools <dir>

The drill scans the collector's JSON-lines files for sentinels after the
collector has been killed and restarted. Whatever the files lost at that
restart was never scanned. This starts the real collector with the
configuration files the drill uses (scripts/ops_drill.py COLLECTOR_CONFIGS;
before that existed, the drill used deploy/observability/otel-collector.yaml
alone), sends one span over OTLP/HTTP, waits until it is in traces.jsonl,
stops and restarts the collector, sends a second span, and checks both are
there. Everything runs in a temporary directory on loopback ports.

Exit 0: both spans present. Exit 1: the first was lost at the restart.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]


def drill_configs() -> list[pathlib.Path]:
    spec = importlib.util.spec_from_file_location("ops_drill", ROOT / "scripts" / "ops_drill.py")
    drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drill)
    return list(getattr(drill, "COLLECTOR_CONFIGS",
                        [ROOT / "deploy" / "observability" / "otel-collector.yaml"]))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(check, what: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError(what)


def send_span(port: int, name: str) -> None:
    now = time.time_ns()
    body = {"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "restart-probe"}}]},
        "scopeSpans": [{"scope": {"name": "probe"}, "spans": [{
            "traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "name": name,
            "kind": 1, "startTimeUnixNano": str(now - 1_000_000), "endTimeUnixNano": str(now)}]}]}]}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/traces", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200, r.status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tools", type=pathlib.Path, required=True)
    args = ap.parse_args()
    configs = drill_configs()
    work = pathlib.Path(tempfile.mkdtemp(prefix="pac-collector-restart-"))
    port, prom = free_port(), free_port()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "PAC_OTEL_RECEIVER": f"127.0.0.1:{port}",
           "PAC_OTEL_PROMETHEUS": f"127.0.0.1:{prom}", "PAC_OTEL_FILE_DIR": str(work)}
    argv = [str(args.tools / "otelcol" / "otelcol-contrib")]
    for c in configs:
        argv += ["--config", str(c)]
    traces = work / "traces.jsonl"
    first, second = (f"probe.marker.{secrets.token_hex(4)}" for _ in range(2))
    result = {"configs": [str(pathlib.Path(c).relative_to(ROOT)) for c in configs]}
    procs = []

    def start():
        with (work / "collector.log").open("ab") as log:
            p = subprocess.Popen(argv, env=env, cwd=work, stdout=log, stderr=subprocess.STDOUT)
        procs.append(p)
        wait_for(lambda: socket.create_connection(("127.0.0.1", port), 0.5).close() or True,
                 "collector listening")
        return p

    def contents() -> str:
        return traces.read_text() if traces.exists() else ""

    try:
        p = start()
        send_span(port, first)
        wait_for(lambda: first in contents(), "first span written")
        result["bytes_before_restart"] = len(contents())
        p.send_signal(signal.SIGTERM)
        p.wait(30)
        start()
        send_span(port, second)
        wait_for(lambda: second in contents(), "second span written")
        text = contents()
        result.update(bytes_after_restart=len(text), first_span_kept=first in text,
                      second_span_written=second in text)
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                p.wait(30)
        for f in work.iterdir():
            f.unlink()
        os.rmdir(work)
    print(json.dumps(result))
    return 0 if result.get("first_span_kept") and result.get("second_span_written") else 1


if __name__ == "__main__":
    sys.exit(main())
