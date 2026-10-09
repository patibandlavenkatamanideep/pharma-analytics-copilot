#!/usr/bin/env python3
"""Reproduce: the live prompt describes any cohort as account ids, and any
previous plan as a follow-up.

app/llm/planner.py build_system_prompt is the only thing the live model sees.
The typed-cohort work landed in OfflinePlanner and in the pipeline, but not
here -- so a PRODUCT cohort is presented to the model as "account ids", and a
fresh question following any answered turn is labelled "This is a FOLLOW-UP".

No model call is made. Exits 0 when the DEFECT IS PRESENT, 1 once fixed.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from app.llm.planner import PlanningContext, build_system_prompt  # noqa: E402

ANCHOR = {"min_mo": 0, "max_mo": 23, "min_wk": 0, "max_wk": 103,
          "max_period_mo": "2026-09", "max_period_qtr": "2026-Q3"}

prompt = build_system_prompt(PlanningContext(
    role="exec", scope_description="all territories and regions",
    wac_authorized=True, reporting_anchor=ANCHOR,
    known_products=["ZENOVAX", "GEMTARA"],
    previous_cohort=["ZENOVAX", "GEMTARA"],
    previous_cohort_dimension="product",
    previous_plan={"metric": "paid_pack_units", "dimensions": ["product"]},
))

mistyped = "account ids: ZENOVAX" in prompt
unconditional_followup = "This is a FOLLOW-UP" in prompt

for line in prompt.splitlines():
    if "account ids" in line.lower() or "FOLLOW-UP" in line:
        print("  >", line.strip()[:110])

print(f"\nproduct cohort described as account ids : {mistyped}")
print(f"previous plan labelled a follow-up      : {unconditional_followup}")
if mistyped or unconditional_followup:
    print("DEFECT PRESENT")
    sys.exit(0)
print("FIXED")
sys.exit(1)
