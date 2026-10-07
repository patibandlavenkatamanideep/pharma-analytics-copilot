#!/usr/bin/env python3
"""Make a pytest JUnit file safe to keep as evidence.

    python3 evidence/probes/sanitize_junit.py <file.junit.xml>

pytest writes failure messages and tracebacks verbatim. One replay on baseline
code raised an AttributeError whose message was the repr of the settings object,
database passwords included. Evidence keeps what a reviewer needs to see that a
test failed and how -- its exception type and a scrubbed first line -- and
drops the rest: traceback text, captured output, properties and the host name.
Values are scrubbed with scripts/record_evidence.py's own rules.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("record_evidence", ROOT / "scripts" / "record_evidence.py")
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)


def first_line(message: str) -> str:
    line = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return str(recorder._scrub(line))[:200]


def sanitize(path: pathlib.Path) -> None:
    tree = ET.parse(path)
    root = tree.getroot()
    for suite in root.iter("testsuite"):
        suite.attrib.pop("hostname", None)
        for props in suite.findall("properties"):
            suite.remove(props)
    for case in root.iter("testcase"):
        for tag in ("system-out", "system-err", "properties"):
            for el in case.findall(tag):
                case.remove(el)
        for tag in ("failure", "error", "skipped"):
            for el in case.findall(tag):
                el.set("message", first_line(el.get("message", "")))
                el.text = None
    tree.write(path, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    for name in sys.argv[1:]:
        sanitize(pathlib.Path(name))
