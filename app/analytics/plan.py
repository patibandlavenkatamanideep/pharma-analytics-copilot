"""The typed analytical plan.

This is the ONLY structure a language model may produce. It deliberately cannot
express a table name, a join, a column, a SQL fragment, a role, a user id or a
scope filter -- those come from server context. Unknown keys are rejected
(`extra="forbid"`), every enum is closed, and the limit is bounded.

A plan that validates is still not authorized: app.auth.policy decides whether
the principal may run it, and the compiler decides what SQL it becomes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_LIMIT = 500


class MetricKey(StrEnum):
    paid_pack_units = "paid_pack_units"
    paid_equivalents = "paid_equivalents"
    pap_volume = "pap_volume"
    total_volume_incl_free = "total_volume_incl_free"
    market_equivalents = "market_equivalents"
    wac_revenue = "wac_revenue"
    brand_market_share = "brand_market_share"
    market_segment_share = "market_segment_share"
    pap_proportion = "pap_proportion"
    volume_growth = "volume_growth"
    share_trend_pp = "share_trend_pp"
    weighted_share_trend = "weighted_share_trend"
    account_count = "account_count"
    facility_count = "facility_count"


class Dimension(StrEnum):
    """Grouping grains. Geography grains resolve through zip_territory only."""
    account = "account"                 # COALESCE(grandparent_org_id, org_id)
    parent = "parent"
    facility = "facility"
    territory = "territory"
    region = "region"
    state = "state"
    product = "product"                 # drug_name
    ndc = "ndc"
    strength = "strength"
    form = "form"
    market_category = "market_category"
    market_subcategory = "market_subcategory"
    specialty = "specialty"
    gpo = "gpo"
    archetype = "archetype"
    is_340b = "is_340b"
    org_status = "org_status"
    data_source = "data_source"
    classification = "classification"   # derived; see A9
    period_mo = "period_mo"
    period_qtr = "period_qtr"
    period_wk = "period_wk"


class NamedWindow(StrEnum):
    """Named business windows. Semantics are fixed by docs/period_offsets.md and
    docs/metric_definitions.md, not by how the phrase reads in English."""
    r3m = "r3m"                     # mo_offset IN (0,1,2)
    r6m_prior = "r6m_prior"         # mo_offset IN (3,4,5) -- the PRECEDING 3 months
    last_6_months = "last_6_months" # mo_offset 0..5 -- a literal six months
    last_month = "last_month"       # mo_offset = 1
    current_month = "current_month" # mo_offset = 0
    last_quarter = "last_quarter"   # mo_offset IN (1,2,3) -- NOT the prior calendar quarter
    r30d = "r30d"                   # wk_offset <= 3 -- four reporting weeks
    current_week = "current_week"   # wk_offset = 0
    ytd = "ytd"
    all_time = "all_time"


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["named", "month_offsets", "week_offsets", "period_labels", "date_range"]
    named: NamedWindow | None = None
    month_offsets: list[Annotated[int, Field(ge=0, le=400)]] | None = None
    week_offsets: list[Annotated[int, Field(ge=0, le=2000)]] | None = None
    period_labels: list[Annotated[str, Field(max_length=16, pattern=r"^\d{4}-(\d{2}|Q[1-4]|W\d{2})$")]] | None = None
    date_from: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    date_to: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "TimeWindow":
        required = {
            "named": self.named,
            "month_offsets": self.month_offsets,
            "week_offsets": self.week_offsets,
            "period_labels": self.period_labels,
            "date_range": self.date_from and self.date_to,
        }[self.kind]
        if not required:
            raise ValueError(f"time window kind {self.kind!r} is missing its value")
        return self


class TriState(StrEnum):
    include = "include"
    exclude = "exclude"
    only = "only"


Name = Annotated[str, Field(min_length=1, max_length=120)]


class Filters(BaseModel):
    """Business filters. Deliberately no territory/region/user field -- geography
    authorization is applied by RLS from server-derived scope, never from a plan.
    A geography value here is a *further narrowing within* the authorized scope
    and is validated against it before use."""

    model_config = ConfigDict(extra="forbid")

    product_names: list[Name] = Field(default_factory=list, max_length=40)
    ndcs: list[Name] = Field(default_factory=list, max_length=40)
    strengths: list[Name] = Field(default_factory=list, max_length=20)
    market_categories: list[Name] = Field(default_factory=list, max_length=20)
    market_subcategories: list[Name] = Field(default_factory=list, max_length=20)
    specialties: list[Name] = Field(default_factory=list, max_length=8)
    classifications: list[Literal["company_brand", "branded_competitor", "generic", "biosimilar"]] = Field(
        default_factory=list, max_length=4
    )

    account_ids: list[Name] = Field(default_factory=list, max_length=200)
    facility_ids: list[Name] = Field(default_factory=list, max_length=500)
    org_archetypes: list[Name] = Field(default_factory=list, max_length=10)
    gpo_names: list[Name] = Field(default_factory=list, max_length=10)
    org_types: list[Literal["Facility", "Parent", "Grandparent", "GPO", "Payer"]] = Field(
        default_factory=list, max_length=5
    )
    # A10: facility-level contribution filtering, disclosed in the answer.
    is_340b: TriState = TriState.include
    active_only: bool = False
    standalone_only: bool = False

    # Narrowing within authorized scope only; validated against it.
    territories: list[Name] = Field(default_factory=list, max_length=20)
    regions: list[Name] = Field(default_factory=list, max_length=10)
    states: list[Annotated[str, Field(min_length=2, max_length=2)]] = Field(
        default_factory=list, max_length=60
    )


class Ranking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["top", "bottom"] = "top"
    # Ranking is always by the plan's own metric. A separate sort metric would
    # be a disclosure channel: ordering by revenue leaks revenue even when the
    # column is hidden, so it is not expressible here.
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 10


class AnalyticalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: MetricKey
    dimensions: list[Dimension] = Field(default_factory=list, max_length=4)
    filters: Filters = Field(default_factory=Filters)
    time: TimeWindow
    comparison: TimeWindow | None = None
    ranking: Ranking | None = None

    # Set when the question cannot be answered as asked. The pipeline returns
    # this text instead of guessing.
    clarification: str | None = Field(default=None, max_length=400)

    # Free-text note the planner may attach describing how it read an ambiguous
    # phrase. Surfaced to the user; never used to build SQL.
    interpretation: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def _coherent(self) -> "AnalyticalPlan":
        needs_comparison = {
            MetricKey.volume_growth,
            MetricKey.share_trend_pp,
            MetricKey.weighted_share_trend,
        }
        if self.metric in needs_comparison and self.comparison is None:
            raise ValueError(f"metric {self.metric} requires a comparison window")
        if self.ranking and not self.dimensions:
            raise ValueError("ranking requires at least one dimension to rank")
        # The dimension list IS the declared grain. A repeated dimension does
        # not refine it -- it produces two identical columns and invites the
        # reader to believe the rows are broken down more finely than they are.
        if len(set(self.dimensions)) != len(self.dimensions):
            duplicated = sorted({d.value for d in self.dimensions
                                 if list(self.dimensions).count(d) > 1})
            raise ValueError(f"dimensions must be distinct; repeated: {duplicated}")
        return self

    def fingerprint(self) -> str:
        import hashlib
        return hashlib.sha256(
            self.model_dump_json(exclude={"interpretation", "clarification"}).encode()
        ).hexdigest()[:16]
