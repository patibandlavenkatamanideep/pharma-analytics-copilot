"""An upper bound on what one Messages request can bill, computed from the
request itself before it is sent.

Review of 1 October 2026, R3. The evaluation budget compared its cap with a
fixed 8,000-token estimate per call. A request larger than the estimate --
a long catalogue, a long conversation, a repair carrying its error -- was
sent anyway and charged afterwards, so the cap could be exceeded. A spend
limit can only hold if every call is admitted against what THAT call can
cost.

Output: the request's own `max_tokens`. The provider stops generating there
(stop_reason "max_tokens"), thinking included, so this is the provider's
guarantee, not an estimate.

Input: no token count is available offline. The provider's counting
endpoint would give an exact figure, but it is a hosted call this code
cannot verify here, so it is not used. Instead:

    bound = UTF-8 bytes of every prompt-bearing field (system, each
            message's content, each tool definition, tool_choice), each
            measured after NFKC normalisation if that is larger
          + PER_MESSAGE_ALLOWANCE per message
          + FRAMING_ALLOWANCE once

It rests on two stated assumptions, not on a provider guarantee:

1. **A token encodes at least one byte of the text it represents.** True of
   byte-level BPE tokenisers. The provider's tokeniser is not published, so
   for it this is an assumption, and the reason reported usage is checked
   against the bound. Where a tokeniser normalises text first (NFKC can
   expand a character), the normalised length is taken if it is larger.
2. **The provider's own framing fits the allowance.** Role markers, the
   tool-use preamble (Anthropic documents 313 to 346 tokens for current
   models) and any re-rendering of the tool schema. Tool definitions are
   measured as indented JSON, which is longer than the compact form.

On the requests the pipeline builds over the full dataset, the bound is
about 24,100 to 24,200 for a first call (measured offline on this code, for
Exec, Director and RAM principals). The live runs of 2026-09-25 billed about
4,670 input tokens per call, on an earlier prompt. So the bound is roughly
five times the measured cost and errs heavily on the safe side. It is
still checked: every call's
reported usage is compared with its reservation, and a call above it stops
the metered run (scripts/run_evals.py, Budget.record_call). That is the
difference between a bound and an estimate. An estimate that is exceeded
quietly raises itself; a bound that is exceeded is a failure, recorded as
one.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

#: Role markers and per-message structure.
PER_MESSAGE_ALLOWANCE = 16
#: The tool-use preamble and anything else the provider adds once per request.
FRAMING_ALLOWANCE = 1_024

#: How the bound was computed, for the evidence record.
METHOD = "utf8-nfkc-bytes+framing-allowance; checked against reported usage"


def _bytes(value: Any) -> int:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return max(len(value.encode()), len(unicodedata.normalize("NFKC", value).encode()))


def input_upper_bound(request: dict[str, Any]) -> int:
    """Input tokens this request can bill, at most, under the assumptions
    above. Everything the provider reads is counted: system text (and so the
    catalogue and any conversation state in it), every message (and so the
    question and any repair), every tool definition and the tool choice."""
    total = FRAMING_ALLOWANCE + _bytes(request.get("system") or "")
    for message in request.get("messages") or []:
        total += PER_MESSAGE_ALLOWANCE + _bytes(message.get("content") or "")
    for tool in request.get("tools") or []:
        total += _bytes(tool)
    if request.get("tool_choice") is not None:
        total += _bytes(request["tool_choice"])
    return total


def output_upper_bound(request: dict[str, Any]) -> int:
    """Output tokens this request can bill, at most: its max_tokens."""
    return int(request["max_tokens"])
