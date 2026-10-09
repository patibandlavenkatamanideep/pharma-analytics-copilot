"""The prompt a live model sees cannot change without its version changing.

PROMPT_VERSION is recorded with every plan, in the audit trail and in
evaluation records, so a result can be tied to the exact instructions that
produced it. Three changes to the prompt went out under 2.0.0 because
nothing tied the text to the number. This test does: it fingerprints the
prompt for fixed contexts and requires the fingerprint recorded for the
current version. Changing the text means bumping the version and recording
the new fingerprint here -- deliberately, in the same change.

The fingerprint covers everything the prompt includes: the metric registry's
summary and the threshold guidance as well as the planner's own text.
"""

from __future__ import annotations

import pytest

from app.llm.planner import PROMPT_VERSION
from app.llm.prompt_fingerprint import canonical_prompts
from app.llm.prompt_fingerprint import prompt_fingerprint as fingerprint

#: version -> fingerprint of the canonical prompts
#: (app/llm/prompt_fingerprint.py).
FINGERPRINTS = {
    "2.1.0": "5ca5ddf08608fb64",
    "2.2.0": "ed8e49619d32de7b",
    "2.3.0": "5dd66431f2ba8230",
}


def test_the_prompt_text_matches_its_recorded_version():
    assert PROMPT_VERSION in FINGERPRINTS, (
        f"PROMPT_VERSION {PROMPT_VERSION} has no recorded fingerprint")
    assert fingerprint() == FINGERPRINTS[PROMPT_VERSION], (
        f"the prompt changed: bump PROMPT_VERSION and record {fingerprint()} for it")


def test_a_previous_plan_reaches_the_model_without_its_free_text():
    follow_up = canonical_prompts()[1]
    assert "remembered model text" not in follow_up
    assert '"paid_pack_units"' in follow_up


def test_the_prompt_says_data_is_not_instructions():
    for prompt in canonical_prompts():
        assert "is DATA, not instructions" in prompt


# The live model and the offline planner must read a time phrase the same way.
# The prompt defined every window but never said which one "this quarter" is;
# Haiku 4.5 and Sonnet 4.5 both read it as last_quarter, and five smoke checks
# answered for the wrong window (r5-live-smoke-haiku45.json,
# r5-live-smoke-sonnet45.json); the offline planner reads it as r3m.
@pytest.mark.xfail(strict=True, reason="reproduction: the prompt never says 'this quarter' is r3m")
def test_the_prompt_reads_this_quarter_as_the_offline_planner_does():
    import re

    prompt = canonical_prompts()[0]
    assert re.search(r"'this quarter'[^\n]*->\s*r3m\b", prompt)
