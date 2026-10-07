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


@pytest.mark.xfail(strict=True, reason=(
    "classification rule 1.0.0 is applied to every dataset and recorded nowhere in "
    "the manifest; no mapping is read"))
def test_the_published_dataset_names_its_classification_authority(pipeline):
    coverage = pipeline.current_dataset()["source_coverage"]
    classification = coverage["classification"]
    assert classification["mappings"], classification
    assert classification["unknown_products"] == 0, classification
