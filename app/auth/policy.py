"""Policy context: who the principal is and what they may do.

Everything here is derived server-side from the supplied users table. Nothing in
this module ever reads a role, user id, territory or pricing flag from the
request body, a header, a cookie payload or a model response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.analytics.plan import AnalyticalPlan
from app.analytics.registry import get_registry

ScopeKind = Literal["global", "region", "territory"]


class AuthorizationError(PermissionError):
    """Raised when a principal may not run a plan. Carries a user-safe message."""

    def __init__(self, message: str, *, alternative: str | None = None) -> None:
        super().__init__(message)
        self.alternative = alternative


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    full_name: str
    role: str
    territory_name: str | None
    region_name: str | None
    can_view_wac: bool

    @property
    def scope_kind(self) -> ScopeKind:
        return {"exec": "global", "director": "region", "ram": "territory"}[self.role]

    @property
    def scope_value(self) -> str | None:
        if self.role == "exec":
            return None
        return self.region_name if self.role == "director" else self.territory_name

    @property
    def wac_authorized(self) -> bool:
        # BOTH conditions. An Exec with can_view_wac = 0 does not get pricing,
        # and a Director with can_view_wac = 1 does not either.
        return self.role == "exec" and self.can_view_wac

    @property
    def scope_description(self) -> str:
        if self.role == "exec":
            return "all territories and regions"
        if self.role == "director":
            return f"the {self.region_name} region"
        return f"the {self.territory_name} territory"

    def fingerprint(self) -> str:
        import hashlib
        raw = f"{self.user_id}|{self.role}|{self.scope_value}|{self.wac_authorized}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_principal(row: dict) -> Principal:
    """Build a principal from a users row, failing closed on anything unexpected."""
    role = (row.get("role") or "").strip().lower()
    if role not in ("exec", "director", "ram"):
        raise AuthorizationError(
            "Your account has no recognised role, so no data can be shown."
        )

    territory = (row.get("territory_name") or "").strip() or None
    region = (row.get("region_name") or "").strip() or None

    # A Director or RAM with no assignment has no provable scope.
    if role == "director" and not region:
        raise AuthorizationError(
            "Your account has no region assigned, so no data can be shown. "
            "Please ask an administrator to set your region."
        )
    if role == "ram" and not territory:
        raise AuthorizationError(
            "Your account has no territory assigned, so no data can be shown. "
            "Please ask an administrator to set your territory."
        )

    can_view_wac = bool(row.get("can_view_wac"))
    if can_view_wac and role != "exec":
        # An inconsistent record is a configuration fault, not a grant.
        can_view_wac = False

    return Principal(
        user_id=row["user_id"], email=row["email"], full_name=row["full_name"],
        role=role, territory_name=territory, region_name=region,
        can_view_wac=can_view_wac,
    )


def authorize(plan: AnalyticalPlan, principal: Principal) -> None:
    """Authorize a plan. Raises AuthorizationError with a usable alternative."""
    registry = get_registry()

    # --- pricing ------------------------------------------------------------
    if registry.requires_wac(plan.metric.value) and not principal.wac_authorized:
        raise AuthorizationError(
            "Pricing data (WAC) is not available at your access level.",
            alternative=(
                "I can show the same breakdown as sales volume instead — either pack "
                "units or equivalents. Volume is not revenue, so the ranking can differ."
            ),
        )

    # --- geography narrowing ------------------------------------------------
    # A geography filter is a narrowing WITHIN scope. Asking for a territory
    # outside the principal's scope is refused rather than silently emptied, so
    # the user learns the restriction instead of seeing a misleading zero.
    if principal.role == "ram":
        allowed = {principal.territory_name}
        requested = set(plan.filters.territories)
        if requested - allowed:
            raise AuthorizationError(
                f"You can only see the {principal.territory_name} territory. "
                f"Data for {', '.join(sorted(requested - allowed))} is outside your access.",
                alternative=f"I can show this for {principal.territory_name} instead.",
            )
        if plan.filters.regions:
            raise AuthorizationError(
                f"You can only see the {principal.territory_name} territory, not a "
                "whole region.",
                alternative=f"I can show this for {principal.territory_name} instead.",
            )

    if principal.role == "director":
        requested_regions = set(plan.filters.regions)
        if requested_regions - {principal.region_name}:
            raise AuthorizationError(
                f"You can only see the {principal.region_name} region. Data for "
                f"{', '.join(sorted(requested_regions - {principal.region_name}))} "
                "is outside your access.",
                alternative=f"I can show this for {principal.region_name} instead.",
            )
        # Territories inside the region are fine; the check against the actual
        # region membership happens in the entity resolver, which knows the
        # zip_territory mapping.


def principal_for_user_id(user_id: str) -> Principal:
    """Build a Principal straight from the supplied users table.

    For server-side tooling (demo generation, evaluation, benchmarking) that
    already runs with database access and has no business authenticating.

    This exists because the alternative was worse: those scripts used to call
    set_credential() to mint a password so they could log in, which silently
    ROTATED the real user's credential every time one of them ran -- once
    invalidating evaluator logins that had already been handed out.

    Authorization is unchanged: the Principal is built by the same
    build_principal() the request path uses, so role, scope and pricing
    permission come from the same place and fail closed the same way. This is
    not a way to bypass a check; it is a way to skip a login that was never
    needed.
    """
    from app.db import auth_transaction

    with auth_transaction() as cur:
        cur.execute(
            "SELECT user_id, email, full_name, role, territory_name, region_name, "
            "can_view_wac FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise AuthorizationError(f"no such user_id: {user_id}")
    return build_principal(row)


def scope_note(principal: Principal, plan: AnalyticalPlan) -> str:
    """One line describing what the answer covers, shown with every result."""
    if principal.role == "exec":
        return "Showing company-wide data (all territories)."
    return f"Showing data for {principal.scope_description} only."
