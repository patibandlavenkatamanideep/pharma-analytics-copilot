"""Loads and freezes the metric registry."""

from __future__ import annotations

import functools
import hashlib
import pathlib
from typing import Any

import yaml

REGISTRY_PATH = pathlib.Path(__file__).with_name("metrics.yaml")


class MetricRegistry:
    def __init__(self, raw: dict[str, Any], digest: str) -> None:
        self.version: str = raw["version"]
        self.digest = digest
        self.components: dict[str, Any] = raw["components"]
        self.metrics: dict[str, Any] = raw["metrics"]

    def get(self, key: str) -> dict[str, Any]:
        try:
            return self.metrics[key]
        except KeyError:
            raise KeyError(f"unknown metric {key!r}") from None

    def component_expr(self, name: str) -> str:
        return self.components[name]["expr"]

    def requires_wac(self, key: str) -> bool:
        """True when this metric -- or anything it is built from -- touches WAC."""
        spec = self.get(key)
        if spec.get("requires_wac"):
            return True
        for child in ("numerator", "denominator", "base_metric", "weight_metric"):
            if (ref := spec.get(child)) and self.requires_wac(ref):
                return True
        return False

    def summary_for_prompt(self) -> str:
        """Compact description injected into EVERY planning request.

        Metric semantics and access rules are never left to retrieval: if a
        restriction only reached the model when some document happened to be
        retrieved, it would not be a restriction.
        """
        lines = [f"metric registry v{self.version}"]
        for key, spec in self.metrics.items():
            bits = [f"- {key}: {spec['label']} [{spec['unit']}]"]
            if spec.get("sources"):
                bits.append(f"sources={spec['sources']}")
            if spec.get("company_only"):
                bits.append("company brand only")
            if spec.get("requires_wac"):
                bits.append("EXEC-ONLY PRICING")
            if spec.get("proposed"):
                bits.append("PROPOSED definition")
            lines.append("  ".join(bits))
            lines.append(f"    {' '.join(spec['description'].split())}")
        return "\n".join(lines)


@functools.lru_cache
def get_registry() -> MetricRegistry:
    text = REGISTRY_PATH.read_text()
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return MetricRegistry(yaml.safe_load(text), digest)
