"""A correct percentage must not be labelled "a count, not a percentage".

Review finding 5d. The intent guard decided whether a metric answers "what
percentage" from a list of three metric names kept in intent.py. The metric
registry grew; the list did not. share_340b, market_segment_share and
volume_growth all return proportions, and a CORRECT answer from any of them
arrived with a note telling the reader the figure was a count.

The rule now reads the unit each metric declares in the registry, so a new
proportion metric is recognised without anyone remembering to add it here.
"""

from __future__ import annotations

import pytest

from app.analytics import intent
from app.analytics.entities import Vocabulary
from app.analytics.plan import AnalyticalPlan, MetricKey
from app.analytics.registry import get_registry

QUESTION = "What percentage of our volume comes from 340B accounts?"
R3M = {"kind": "named", "named": "r3m"}
PRIOR = {"kind": "named", "named": "r6m_prior"}


def proportion_gap(metric: str) -> bool:
    spec = get_registry().get(metric)
    plan = AnalyticalPlan.model_validate({
        "metric": metric, "time": R3M,
        **({"comparison": PRIOR} if spec.get("kind") == "period_change" else {})})
    found = intent.find_gaps(QUESTION, plan, Vocabulary())
    return any(g.kind == "unsupported_proportion" for g in found)


@pytest.mark.parametrize("metric", [
    # The three the stale list forgot:
    "share_340b", "market_segment_share", "volume_growth",
    # and the ones it had:
    "brand_market_share", "pap_proportion", "share_trend_pp",
])
def test_a_proportion_metric_is_not_called_a_count(metric):
    assert not proportion_gap(metric)


@pytest.mark.parametrize("metric", ["paid_pack_units", "paid_equivalents",
                                    "account_count", "facility_count_all"])
def test_a_count_answering_a_percentage_question_is_still_disclosed(metric):
    """The disclosure exists for a reason: 59,419 packs is not a percentage."""
    assert proportion_gap(metric)


def test_every_registered_metric_is_classified_by_its_declared_unit():
    """No metric depends on appearing in a list. If a new metric declares
    unit: ratio, it is a proportion; nothing else needs to change."""
    for metric in MetricKey:
        unit = get_registry().get(metric.value).get("unit")
        assert proportion_gap(metric.value) is (unit not in intent.PROPORTION_UNITS), metric
