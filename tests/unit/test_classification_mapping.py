"""The classification authority (app/data/classification.py, rule 2.0.0).

The source marks only our own brands. Every other class comes from a
versioned mapping, or is unknown; a mapping that contradicts the source, or
that cannot be read as one, is refused rather than half-applied.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.analytics.plan import AnalyticalPlan
from app.analytics.render import _headline, check_quality
from app.data.classification import (SUPPLIED_MAPPING, MappingError, classify,
                                     read_mapping)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def mapping_file(tmp_path, entries, **header):
    doc = {"mapping": "acme", "version": "1.0.0", "authority": "a customer file",
           **header, "entries": entries}
    path = tmp_path / "product_classification.json"
    path.write_text(json.dumps(doc))
    return path


def entry(name, sub, cls):
    return {"drug_name": name, "market_subcategory": sub, "classification": cls}


def test_a_mapped_product_takes_the_mapping_class_and_names_it(tmp_path):
    m = read_mapping(mapping_file(tmp_path, [entry("praxolone", "Anti-IL", "generic")]))
    got = classify("PRAXOLONE", 0, "Anti-IL", m)
    assert got.classification == "generic" and got.authority == "mapping"
    assert m.ref in got.derivation and m.sha256[:12] in m.ref


def test_the_same_name_in_another_market_is_not_mapped(tmp_path):
    m = read_mapping(mapping_file(tmp_path, [entry("PRAXOLONE", "Anti-IL", "generic")]))
    assert classify("PRAXOLONE", 0, "Anti-TNF", m).classification == "unknown"


@pytest.mark.parametrize("flag,cls", [(1, "generic"), (0, "company_brand")])
def test_a_mapping_that_contradicts_the_source_is_refused(tmp_path, flag, cls):
    m = read_mapping(mapping_file(tmp_path, [entry("VELTRIQ", "Anti-IL", cls)]))
    with pytest.raises(MappingError, match="brand_flag"):
        classify("VELTRIQ", flag, "Anti-IL", m)


@pytest.mark.parametrize("entries,match", [
    ([entry("A", "S", "generic"), entry("a", "S", "biosimilar")], "listed twice"),
    ([entry("A", "S", "unknown")], "leave a product out"),
    ([entry("A", "S", "branded")], "not one of"),
    ([{"drug_name": "A", "classification": "generic"}], "market_subcategory"),
    (["A"], "not an object"),
])
def test_a_mapping_that_cannot_be_an_authority_is_refused(tmp_path, entries, match):
    with pytest.raises(MappingError, match=match):
        read_mapping(mapping_file(tmp_path, entries))


def test_a_mapping_without_a_version_or_authority_is_refused(tmp_path):
    path = mapping_file(tmp_path, [], version="")
    with pytest.raises(MappingError, match="'version'"):
        read_mapping(path)
    path.write_text("{not json")
    with pytest.raises(MappingError, match="not JSON"):
        read_mapping(path)


def test_the_supplied_mapping_is_exactly_the_supplied_documents_tables():
    """Every product docs/market_classification.md lists, in its market, and
    nothing else: the brand column is ours, the competitors are the rest."""
    listed: dict[tuple[str, str], str] = {}
    in_table = False
    for line in (ROOT / "docs" / "market_classification.md").read_text().splitlines():
        if line.startswith("| market_category | market_subcategory | NovaPharma Brand |"):
            in_table = True
            continue
        if not line.startswith("|"):
            in_table = False
        if not in_table or line.startswith("|---"):
            continue
        _, sub, brand, competitors = (c.strip() for c in line.strip("|").split("|"))
        if brand != "—":
            listed[(brand, sub)] = "company_brand"
        for name in (c.strip() for c in competitors.split(",")):
            listed[(name, sub)] = "competitor"
    mapping = read_mapping(SUPPLIED_MAPPING)
    assert set(mapping.entries) == set(listed)
    for key, cls in mapping.entries.items():
        assert (cls == "company_brand") == (listed[key] == "company_brand"), key


# ---------------------------------------------------------------------------
# What an answer says
# ---------------------------------------------------------------------------

class FakeQuery:
    def __init__(self):
        self.quality_checks = ["derived_classification"]
        self.metric_label = "market segment share"
        self.unit = "ratio"


PLAN = AnalyticalPlan.model_validate({
    "metric": "market_segment_share", "filters": {"classifications": ["generic"]},
    "time": {"kind": "named", "named": "r3m"}})


def test_the_answer_names_the_mapping_and_counts_unknown_products():
    coverage = {"classification": {"mappings": ["acme 1.0.0 (sha256 0123456789ab)"],
                                   "unknown_products": 2}}
    text = " ".join(check_quality([{"value": 0.2}], FakeQuery(), PLAN, coverage))
    assert "acme 1.0.0 (sha256 0123456789ab)" in text
    assert "2 product(s) have no class" in text


def test_without_a_mapping_the_answer_says_a_segment_cannot_be_measured():
    coverage = {"classification": {"mappings": [], "unknown_products": 3}}
    text = " ".join(check_quality([{"value": None}], FakeQuery(), PLAN, coverage))
    assert "No classification mapping was supplied" in text


def test_an_unrecorded_authority_is_said_to_be_unrecorded():
    text = " ".join(check_quality([{"value": 0.2}], FakeQuery(), PLAN, {}))
    assert "did not record which classification authority" in text


def test_a_share_with_unknown_volume_is_stated_as_a_range():
    row = {"numerator": 10.0, "denominator": 100.0, "value": 0.1,
           "unclassified": 25.0, "value_upper": 0.35}
    assert _headline([row], FakeQuery(), PLAN) == (
        "Market segment share is between 10.00% and 35.00%: part of this market's "
        "volume is of unknown class.")
    text = " ".join(check_quality([row], FakeQuery(), PLAN, {}))
    assert "10.00% to 35.00%" in text


def test_a_market_all_of_unknown_class_has_no_share():
    row = {"numerator": None, "denominator": 80.0, "value": None,
           "unclassified": 80.0, "value_upper": None}
    assert "unavailable" in _headline([row], FakeQuery(), PLAN)
    text = " ".join(check_quality([row], FakeQuery(), PLAN, {}))
    assert "unavailable rather than zero" in text
