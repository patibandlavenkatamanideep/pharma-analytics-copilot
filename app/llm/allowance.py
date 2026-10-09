"""The website's model allowance: one limit for every user, worker and replica.

Serving requests were bounded by per-user request quotas, not by spend, and an
evaluation script's token cap (scripts/run_evals.py's Budget, in that one
process's memory) never reached them. This is the serving pipeline's meter
(Pipeline.spend, PlanningContext.spend), with the same interface the planner
already uses, kept where every process sees it: one row of
app_meta.model_allowance, in micro-dollars.

* reserve(): before each model call -- every attempt, the repair included --
  the request's own upper bound (its input bound and its max_tokens), priced
  at the configured rates, is added to the committed total in one atomic
  UPDATE, only if the total stays within the limit. Concurrent workers and
  replicas therefore cannot both take the last of it.
* record_call(): after the call, the reservation is replaced by the cost of
  the token usage the provider reported, at the configured prices; usage it
  did not report stays charged at the reservation. This is the application's
  own calculation, not the AWS invoice, which remains the truth.
* Who can change the row: the serving processes, through the auth role,
  which may update every counter -- this is an application-level control,
  trusted to the serving process. A compromised serving process could reset
  it, but it also holds the Bedrock permission itself; the backstops against
  that are the task role's scope (one model), the AWS budget alert on
  Bedrock, and removing that permission (docs/RUNBOOK.md, "Model allowance").
* release(): a call reserved but never sent gives its reservation back.
* A call whose reported usage exceeds its bound is a broken invariant: it is
  recorded, this run stops, and no further call is reserved anywhere until an
  operator looks (docs/RUNBOOK.md, "Model allowance").
* Any failure to reach the row refuses the call: an unknown spend is never
  treated as room to spend.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

ALLOWANCE_ID = "serving"


class SharedAllowance:
    def __init__(self, limit_usd: float, input_usd_per_mtok: float, output_usd_per_mtok: float,
                 allowance_id: str = ALLOWANCE_ID):
        # The same rules as the settings (app/config.py), for a meter built
        # directly: a free or refunding price would make the limit meaningless.
        if not (math.isfinite(limit_usd) and limit_usd >= 0):
            raise ValueError("the model allowance must be a finite number of dollars, zero or more")
        for price in (input_usd_per_mtok, output_usd_per_mtok):
            if not (math.isfinite(price) and price > 0):
                raise ValueError("a model price must be a finite number above zero")
        self.limit_micro = int(Decimal(str(limit_usd)) * 1_000_000)
        self.rate_in = Decimal(str(input_usd_per_mtok))
        self.rate_out = Decimal(str(output_usd_per_mtok))
        self.allowance_id = allowance_id
        self._violated = False

    def micro_usd(self, input_tokens: int, output_tokens: int) -> int:
        """Tokens at USD per million tokens, in micro-dollars, rounded up."""
        return math.ceil(input_tokens * self.rate_in + output_tokens * self.rate_out)

    def reserve(self, input_tokens: int, output_tokens: int) -> bool:
        """May a call that can bill up to this much be sent?"""
        from app.db import auth_transaction

        cost = self.micro_usd(input_tokens, output_tokens)
        try:
            with auth_transaction(timeout=3) as cur:
                cur.execute(
                    "UPDATE app_meta.model_allowance "
                    "   SET committed_microusd = committed_microusd + %s, calls = calls + 1, "
                    "       updated_at = now() "
                    " WHERE allowance_id = %s AND bound_violations = 0 "
                    "   AND committed_microusd + %s <= %s "
                    "RETURNING committed_microusd",
                    (cost, self.allowance_id, cost, self.limit_micro))
                if cur.fetchone() is not None:
                    return True
                cur.execute("UPDATE app_meta.model_allowance SET refused = refused + 1, "
                            "updated_at = now() WHERE allowance_id = %s", (self.allowance_id,))
                return False
        except Exception:
            return False

    def record_call(self, usage: Any, reserved: tuple[int, int]) -> None:
        from app.db import auth_transaction

        reserved_in, reserved_out = reserved
        reported_in = getattr(usage, "input_tokens", None)
        reported_out = getattr(usage, "output_tokens", None)
        charged_in = reported_in if reported_in is not None else reserved_in
        charged_out = reported_out if reported_out is not None else reserved_out
        violation = charged_in > reserved_in or charged_out > reserved_out
        unreported = reported_in is None or reported_out is None
        delta = self.micro_usd(charged_in, charged_out) - self.micro_usd(reserved_in, reserved_out)
        if violation:
            self._violated = True
        with auth_transaction(timeout=3) as cur:
            cur.execute(
                "UPDATE app_meta.model_allowance "
                "   SET committed_microusd = GREATEST(committed_microusd + %s, 0), "
                "       bound_violations = bound_violations + %s, "
                "       unreported_calls = unreported_calls + %s, updated_at = now() "
                " WHERE allowance_id = %s",
                (delta, int(violation), int(unreported), self.allowance_id))

    def release(self, reserved: tuple[int, int]) -> None:
        """A reserved call that was never sent: its reservation comes back."""
        from app.db import auth_transaction

        cost = self.micro_usd(*reserved)
        with auth_transaction(timeout=3) as cur:
            cur.execute(
                "UPDATE app_meta.model_allowance "
                "   SET committed_microusd = GREATEST(committed_microusd - %s, 0), "
                "       calls = GREATEST(calls - 1, 0), updated_at = now() "
                " WHERE allowance_id = %s", (cost, self.allowance_id))

    @property
    def violated(self) -> bool:
        return self._violated


def from_settings(settings: Any) -> SharedAllowance | None:
    """The configured allowance, or None when none is configured. A limit
    without both rates cannot be priced, so it refuses every call (a limit of
    zero) rather than letting calls through unpriced."""
    limit = settings.llm_spend_limit_usd
    if limit is None:
        return None
    rate_in, rate_out = settings.llm_input_usd_per_mtok, settings.llm_output_usd_per_mtok
    if rate_in is None or rate_out is None:
        return SharedAllowance(0, 1, 1)
    return SharedAllowance(limit, rate_in, rate_out)
