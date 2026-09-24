"""The registry has to be self-consistent before anything compiles it.

A ratio that does not say what its denominator is a proportion OF is the
defect that produced 20/175 instead of 20/130 for PAP share. The compiler was
deciding it, identically, for every ratio. Making the declaration mandatory
means the next ratio metric cannot inherit the wrong one by accident.
"""

from __future__ import annotations

import copy

import pytest

from app.analytics.registry import (
    DENOMINATOR_POPULATIONS,
    MetricRegistry,
    RegistryError,
    get_registry,
)


def _raw():
    """The real registry, as data, so these tests track the real file."""
    registry = get_registry()
    return {
        "version": registry.version,
        "components": copy.deepcopy(registry.components),
        "metrics": copy.deepcopy(registry.metrics),
    }


def test_the_shipped_registry_is_valid():
    assert get_registry().version


def test_every_ratio_declares_its_denominator_population():
    for key, spec in get_registry().metrics.items():
        if spec.get("kind") == "ratio":
            assert spec.get("denominator_population") in DENOMINATOR_POPULATIONS, key


def test_the_two_ratios_declare_different_populations():
    """Otherwise the distinction is decorative."""
    metrics = get_registry().metrics
    assert metrics["brand_market_share"]["denominator_population"] == "surrounding_market"
    assert metrics["pap_proportion"]["denominator_population"] == "same_population"


def test_a_ratio_without_a_declared_population_is_refused_at_load():
    raw = _raw()
    del raw["metrics"]["pap_proportion"]["denominator_population"]
    with pytest.raises(RegistryError, match="denominator_population"):
        MetricRegistry(raw, "test")


def test_an_unknown_population_is_refused_at_load():
    raw = _raw()
    raw["metrics"]["pap_proportion"]["denominator_population"] = "whatever"
    with pytest.raises(RegistryError, match="expected one of"):
        MetricRegistry(raw, "test")


def test_a_ratio_naming_a_metric_that_does_not_exist_is_refused():
    raw = _raw()
    raw["metrics"]["pap_proportion"]["denominator"] = "no_such_metric"
    with pytest.raises(RegistryError, match="unknown denominator"):
        MetricRegistry(raw, "test")


def test_a_non_ratio_metric_needs_no_population():
    raw = _raw()
    assert "denominator_population" not in raw["metrics"]["paid_pack_units"]
    MetricRegistry(raw, "test")      # must not raise
