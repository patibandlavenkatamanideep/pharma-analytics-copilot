"""Dataset manifest.

The reporting anchor is derived from the loaded data, never from the server
clock, so relative periods stay stable regardless of when a question is asked.
A snapshot is queryable only once load_state = 'published'.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass, field
from typing import Any

MAPPING_VERSION = "1.0.0"


def file_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class LoadReport:
    dataset_id: str
    load_mode: str
    source_hashes: dict[str, str] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)
    reporting_anchor: dict[str, Any] = field(default_factory=dict)
    source_coverage: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def warn(self, code: str, message: str, **detail: Any) -> None:
        self.warnings.append({"code": code, "message": message, **detail})
