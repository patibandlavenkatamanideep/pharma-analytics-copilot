"""Where a product's classification comes from, on the release dataset.

The supplied data marks only brand_flag; generic, biosimilar and branded
competitor are not columns. The supplied documentation names every product
in its market (docs/market_classification.md). A published dataset must say
which authority classified its products, so an answer can say it too.
"""

from __future__ import annotations

import pytest

from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]


def test_the_published_dataset_names_its_classification_authority(pipeline):
    coverage = pipeline.current_dataset()["source_coverage"]
    classification = coverage["classification"]
    assert classification["mappings"], classification
    assert classification["unknown_products"] == 0, classification


def test_every_supplied_product_is_classified_by_the_source_or_the_supplied_mapping():
    import hashlib

    from app.data.classification import SUPPLIED_MAPPING, read_mapping
    from app.db import owner_transaction

    mapping = read_mapping(SUPPLIED_MAPPING)
    with owner_transaction() as cur:
        cur.execute("SELECT p.drug_name, p.market_subcategory, p.brand_flag, c.classification, "
                    "c.authority, c.mapping_ref, c.rule_version FROM products p "
                    "JOIN app_ref.product_classification c ON c.ndc = p.ndc")
        rows = cur.fetchall()
    assert rows
    digest = hashlib.sha256(SUPPLIED_MAPPING.read_bytes()).hexdigest()
    for r in rows:
        assert r["rule_version"] == "2.0.0"
        if r["brand_flag"] == 1:
            assert (r["classification"], r["authority"]) == ("company_brand", "source"), r
            continue
        assert r["authority"] == "mapping" and digest[:12] in r["mapping_ref"], r
        assert r["classification"] == mapping.entries[(r["drug_name"].upper(),
                                                       r["market_subcategory"])], r
