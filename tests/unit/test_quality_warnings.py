"""A warning must be a claim about THIS dataset, checked against evidence.

Two of these warnings were asserted unconditionally, from the metric alone.
That makes them decoration: they appear whether or not the thing they describe
is true, and on the coherent fixture -- which deliberately reports a company
brand in market data -- the answer carried a warning saying it does not. A
caveat that is always present teaches the reader to skip caveats, which costs
more than the caveat was worth.
"""

from __future__ import annotations

from app.analytics.plan import AnalyticalPlan
from app.analytics.render import check_quality


class FakeQuery:
    def __init__(self, checks):
        self.quality_checks = checks


PLAN = AnalyticalPlan.model_validate({
    "metric": "brand_market_share",
    "time": {"kind": "named", "named": "r3m"},
})


def warnings_for(checks, coverage):
    return check_quality([{"value": 0.4}], FakeQuery(checks), PLAN, coverage)


def joined(checks, coverage):
    return " ".join(warnings_for(checks, coverage)).lower()


# ---------------------------------------------------------------------------
# Market completeness
# ---------------------------------------------------------------------------

def test_competitor_only_warning_fires_when_the_data_says_so():
    text = joined(["denominator_completeness"], {"market_data_company_rows": 0})
    assert "only competitor products" in text


def test_competitor_only_warning_is_silent_when_the_data_disagrees():
    """The fixture reports 30 units of our own brand in market data."""
    text = joined(["denominator_completeness"], {"market_data_company_rows": 1})
    assert "only competitor products" not in text, (
        "warned that market data has no company rows when it has some"
    )


def test_unmeasured_completeness_is_not_reported_as_fine():
    text = joined(["denominator_completeness"], {})
    assert "not measured" in text
    assert "only competitor products" not in text


# ---------------------------------------------------------------------------
# Conversion factors
# ---------------------------------------------------------------------------

def test_missing_conversion_factors_are_disclosed_as_a_lower_bound():
    text = joined(["conversion_factor_coverage"],
                  {"products_missing_conversion_factor": 3})
    assert "3 product" in text
    assert "lower bound" in text
    assert "excluded" in text


def test_no_missing_conversion_factors_produces_no_noise():
    assert warnings_for(["conversion_factor_coverage"],
                        {"products_missing_conversion_factor": 0}) == []


def test_unmeasured_conversion_coverage_is_not_reported_as_fine():
    text = joined(["conversion_factor_coverage"], {})
    assert "not measured" in text


# ---------------------------------------------------------------------------
# A metric that declares no checks says nothing
# ---------------------------------------------------------------------------

def test_a_metric_without_checks_emits_no_warnings():
    assert warnings_for([], {"market_data_company_rows": 0}) == []
