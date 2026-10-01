"""A fingerprint of the prompt a live model sees.

The system prompt for fixed, canonical contexts -- a fresh question, a
follow-up carrying a previous plan, a frozen cohort -- hashed. It covers
everything the prompt includes: the planner's text, the metric registry's
summary and the threshold guidance. Evaluation records carry it, so a live
result names the exact instructions that produced it, and
tests/unit/test_prompt_version.py ties it to PROMPT_VERSION.
"""

from __future__ import annotations

import hashlib

from app.conversation.continuity import Cohort
from app.conversation.continuity import resolve as resolve_continuity
from app.llm.planner import PlanningContext, build_system_prompt

ANCHOR = {"min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}

def canonical_prompts() -> list[str]:
    previous = {"metric": "paid_pack_units", "dimensions": ["account"],
                "time": {"kind": "named", "named": "r3m"},
                "ranking": {"direction": "top", "limit": 5},
                "interpretation": "remembered model text"}
    cohort = Cohort(dimension="account", ids=("A1", "A2"), complete=True, total_available=2)
    contexts = [
        PlanningContext(role="exec", scope_description="all", wac_authorized=True,
                        reporting_anchor=ANCHOR, known_products=["ZENOVAX"],
                        continuity=resolve_continuity("total volume last quarter")),
        PlanningContext(role="ram", scope_description="New York Metro",
                        wac_authorized=False, reporting_anchor=ANCHOR,
                        known_products=["ZENOVAX"], known_territories=["New York Metro"],
                        named_accounts=[("Acme Health", "GP1")], previous_plan=previous,
                        continuity=resolve_continuity("break that down by product",
                                                      previous_plan=previous)),
        PlanningContext(role="director", scope_description="Northeast",
                        wac_authorized=False, reporting_anchor=ANCHOR,
                        previous_plan=previous, previous_cohort=["A1", "A2"],
                        previous_cohort_dimension="account",
                        continuity=resolve_continuity("show those same accounts by month",
                                                      previous_plan=previous, cohort=cohort)),
    ]
    return [build_system_prompt(c) for c in contexts]


def prompt_fingerprint() -> str:
    return hashlib.sha256("\n\x00\n".join(canonical_prompts()).encode()).hexdigest()[:16]
