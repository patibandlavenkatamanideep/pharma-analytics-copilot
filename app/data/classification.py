"""Derived product classification.

brand_flag = 0 means "competitor/generic" and includes branded competitors, so
it cannot answer a question about generics. This module owns the documented
rule that turns drug names into an explicit classification, and it is versioned
so an answer can state which rule produced it.
"""

from __future__ import annotations

RULE_VERSION = "1.0.0"


def classify(drug_name: str, brand_flag: int) -> tuple[str, str]:
    """Return (classification, derivation) for one product."""
    name = (drug_name or "").strip().upper()
    if brand_flag == 1:
        return "company_brand", "brand_flag=1"
    if name.endswith(" BIOSIMILAR"):
        return "biosimilar", "name suffix ' BIOSIMILAR'"
    if name.endswith(" GENERIC"):
        return "generic", "name suffix ' GENERIC'"
    return "branded_competitor", "brand_flag=0 with no generic/biosimilar suffix"
