"""Question -> typed plan.

The model's ONLY job is to choose a metric key, some dimensions, some filter
values and a time window from closed vocabularies. It never sees a table name,
never writes SQL, and never decides what the principal is allowed to see -- a
plan it produces is authorized afterwards by app.auth.policy and executed
through a connection whose privileges were fixed before the model ran.

Two implementations share one interface:

  BedrockPlanner  -- AWS Bedrock, structured output via a forced tool call.
  OfflinePlanner  -- deterministic keyword rules, so the whole pipeline stays
                     testable and demonstrable with no credentials and no spend.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from app.analytics.plan import AnalyticalPlan, Dimension, MetricKey
from app.analytics.registry import get_registry
from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class PlannerError(RuntimeError):
    """The question could not be turned into a plan."""


@dataclass
class PlanningContext:
    """Everything the planner is allowed to know.

    Note what is absent: no user id, no session token, no SQL, no database
    credentials. `role` and `scope_description` are present only so the planner
    can pick a volume metric instead of a pricing one and phrase a useful
    clarification -- they are not how the restriction is enforced.
    """
    role: str
    scope_description: str
    wac_authorized: bool
    reporting_anchor: dict[str, Any]
    known_products: list[str] = field(default_factory=list)
    known_subcategories: list[str] = field(default_factory=list)
    known_categories: list[str] = field(default_factory=list)
    known_gpos: list[str] = field(default_factory=list)
    known_archetypes: list[str] = field(default_factory=list)
    known_territories: list[str] = field(default_factory=list)
    known_regions: list[str] = field(default_factory=list)
    previous_plan: dict[str, Any] | None = None
    previous_cohort: list[str] = field(default_factory=list)


class Planner(Protocol):
    def plan(self, question: str, context: PlanningContext) -> AnalyticalPlan: ...


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_system_prompt(context: PlanningContext) -> str:
    registry = get_registry()
    anchor = context.reporting_anchor

    # Metric and access rules are included in EVERY request rather than
    # retrieved. A restriction that only arrives when some document happens to
    # be retrieved is not a restriction.
    parts = [
        "You turn a commercial analytics question about a pharmaceutical sales "
        "database into a structured query plan. You never write SQL.",
        "",
        "Emit exactly one call to the emit_plan tool. Choose only from the "
        "enumerated metric keys, dimensions and window names. Never invent a key.",
        "",
        registry.summary_for_prompt(),
        "",
        "REPORTING WINDOWS -- these are definitions, not English readings:",
        "  r3m            = the rolling 3 months (offsets 0,1,2)",
        "  r6m_prior      = the 3 months PRECEDING r3m (offsets 3,4,5). This is the",
        "                   business 'R6M' comparison window. It is NOT six months.",
        "  last_6_months  = a literal six months (offsets 0..5)",
        "  last_quarter   = offsets 1,2,3. This is NOT the previous calendar quarter.",
        "                   If the user clearly means a calendar quarter such as Q2,",
        "                   use period_labels instead.",
        "  last_month     = offset 1;  current_month = offset 0;  r30d = last 4 weeks",
        "For an explicit calendar quarter or year use kind='period_labels' with labels",
        "like '2026-Q2'. Use kind='date_range' ONLY when the user gives explicit dates.",
        "",
        "RULES THAT MUST NOT BE BROKEN:",
        "- 'sales', 'volume' and 'demand' mean PAID demand: the distributor source,",
        "  company brand only. Never include hub_dispense unless the user asks for",
        "  free drug, PAP or 'including free drug'.",
        "- Market share ALWAYS uses brand_market_share. Never build it from two",
        "  volume metrics.",
        "- 'Accounts' means the account dimension (top-level health system).",
        "- Growth is volume_growth and needs a comparison window. A change in market",
        "  share is share_trend_pp (percentage points), not volume_growth.",
        "- Set clarification (and nothing else) when the question is genuinely",
        "  ambiguous or asks for something the metric list cannot express.",
        "- Use interpretation to state how you read an ambiguous phrase.",
        "",
        f"Data covers month offsets {anchor.get('min_mo')}..{anchor.get('max_mo')} and "
        f"week offsets {anchor.get('min_wk')}..{anchor.get('max_wk')}. "
        f"The latest reporting month is {anchor.get('max_period_mo')} and the latest "
        f"quarter is {anchor.get('max_period_qtr')}. Offsets are relative to the data "
        "refresh, not to today's date.",
    ]

    if not context.wac_authorized:
        parts += [
            "",
            "PRICING: this user may NOT see revenue or WAC. Do not choose wac_revenue. "
            "If they ask for revenue or dollars, choose paid_pack_units instead and set "
            "interpretation to say that volume is shown because pricing is restricted. "
            "Never rank or filter by revenue for this user.",
        ]

    if context.known_products:
        parts += ["", f"Known products: {', '.join(context.known_products)}"]
    if context.known_subcategories:
        parts += [f"Market subcategories: {', '.join(context.known_subcategories)}"]
    if context.known_categories:
        parts += [f"Market categories: {', '.join(context.known_categories)}"]
    if context.known_gpos:
        parts += [f"GPOs: {', '.join(context.known_gpos)}"]
    if context.known_archetypes:
        parts += [f"Organization archetypes: {', '.join(context.known_archetypes)}"]
    if context.known_territories:
        parts += [f"Territories you may reference: {', '.join(context.known_territories)}"]
    if context.known_regions:
        parts += [f"Regions you may reference: {', '.join(context.known_regions)}"]

    if context.previous_plan:
        parts += [
            "",
            "This is a FOLLOW-UP. The previous plan was:",
            json.dumps(context.previous_plan, indent=2, default=str),
            "Carry forward everything the user did not change. 'Break that down by X' "
            "adds a dimension and keeps the filters and window. 'Compare to last year' "
            "adds a comparison and keeps the population. 'Exclude 340B' sets "
            "filters.is_340b='exclude' and changes nothing else.",
        ]
        if context.previous_cohort:
            parts += [
                f"The previous answer was about these account ids: "
                f"{', '.join(context.previous_cohort)}. If the user says 'those "
                "accounts' or 'the same accounts', put exactly these ids in "
                "filters.account_ids and drop the ranking, so the cohort is frozen "
                "rather than re-ranked.",
            ]

    return "\n".join(parts)


def _plan_tool_schema() -> dict[str, Any]:
    schema = AnalyticalPlan.model_json_schema()
    schema.pop("title", None)
    return schema


# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------

class BedrockPlanner:
    def __init__(self, settings: Settings | None = None) -> None:
        from anthropic import AnthropicBedrockMantle

        self.settings = settings or get_settings()
        self.model_id = self.settings.bedrock_model_id
        self._client = AnthropicBedrockMantle(
            aws_region=self.settings.bedrock_region,
            timeout=self.settings.llm_timeout_s,
            max_retries=2,
        )
        self.last_usage: dict[str, int] = {}

    def plan(self, question: str, context: PlanningContext) -> AnalyticalPlan:
        system = build_system_prompt(context)
        tool = {
            "name": "emit_plan",
            "description": "Emit the structured analytical plan for this question.",
            "input_schema": _plan_tool_schema(),
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

        raw, err = self._attempt(system, messages, tool)
        if raw is not None:
            return raw

        # One bounded repair. The model is told exactly what failed validation;
        # it does not get to widen the schema, only to satisfy it.
        messages += [
            {"role": "assistant", "content": "I produced an invalid plan."},
            {
                "role": "user",
                "content": (
                    f"That plan failed validation:\n{err}\n"
                    "Emit emit_plan again, valid this time. Use only enumerated values."
                ),
            },
        ]
        raw, err2 = self._attempt(system, messages, tool)
        if raw is not None:
            return raw
        raise PlannerError(f"planner produced an invalid plan twice: {err2}")

    def _attempt(
        self, system: str, messages: list[dict[str, Any]], tool: dict[str, Any]
    ) -> tuple[AnalyticalPlan | None, str]:
        response = self._client.messages.create(
            model=self.model_id,
            max_tokens=self.settings.llm_max_tokens,
            system=system,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_plan"},
            output_config={"effort": self.settings.llm_effort},
        )
        usage = getattr(response, "usage", None)
        if usage:
            self.last_usage = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "emit_plan":
                try:
                    return AnalyticalPlan.model_validate(block.input), ""
                except ValidationError as exc:
                    return None, _short_validation_error(exc)
        return None, "the model did not call emit_plan"


def _short_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors()[:6]:
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"{loc}: {err['msg']}")
    return "; ".join(lines)


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------

class OfflinePlanner:
    """Deterministic keyword planner.

    This is NOT a natural-language model and is never presented as one. It makes
    the compiler, authorization, execution and rendering layers testable and
    demonstrable without credentials or spend, and it gives CI a stable planner
    so a failing test means the pipeline broke, not that a model drifted.
    """

    WINDOWS = [
        (r"\blast quarter\b", "last_quarter"),
        (r"\br3m\b|\blast (?:three|3) months\b|\bthis quarter\b", "r3m"),
        (r"\br6m\b|\bprior (?:three|3) months\b|\bprevious quarter\b", "r6m_prior"),
        (r"\blast (?:six|6) months\b|\bpast (?:six|6) months\b", "last_6_months"),
        (r"\blast month\b", "last_month"),
        (r"\bthis month\b|\bcurrent month\b", "current_month"),
        (r"\br30d\b|\blast (?:four|4) weeks\b|\blast 30 days\b", "r30d"),
        (r"\bthis week\b|\bcurrent week\b", "current_week"),
        (r"\byear to date\b|\bytd\b", "ytd"),
        (r"\ball time\b|\ball history\b", "all_time"),
    ]

    DIMENSION_WORDS = [
        (r"\bby territor\w*|\bper territor\w*", Dimension.territory),
        (r"\bby region\b|\bper region\b", Dimension.region),
        (r"\bby state\b", Dimension.state),
        (r"\bby month\b|\bmonthly\b|\bmonth over month\b", Dimension.period_mo),
        (r"\bby quarter\b|\bquarterly\b|\bquarter over quarter\b", Dimension.period_qtr),
        (r"\bby week\b|\bweekly\b", Dimension.period_wk),
        (r"\bby product\b|\bper product\b|\bby drug\b|\beach product\b", Dimension.product),
        (r"\bby ndc\b", Dimension.ndc),
        (r"\bby strength\b", Dimension.strength),
        (r"\bby gpo\b|\bper gpo\b", Dimension.gpo),
        (r"\bby archetype\b|hospital vs clinic", Dimension.archetype),
        (r"\bby specialt\w*", Dimension.specialty),
        (r"\bby market categor\w*", Dimension.market_category),
        (r"\bby (?:market )?subcategor\w*", Dimension.market_subcategory),
        (r"\bby (?:data )?source\b", Dimension.data_source),
        (r"\bby facilit\w*", Dimension.facility),
        (r"\bby account\b|\bby health system\b|\baccounts?\b", Dimension.account),
    ]

    def plan(self, question: str, context: PlanningContext) -> AnalyticalPlan:
        q = question.lower().strip()
        prev = context.previous_plan or {}

        metric = self._metric(q, context)
        dimensions = self._dimensions(q, metric)
        window = self._window(q, prev)
        filters = self._filters(q, context, prev)
        ranking = self._ranking(q, dimensions)
        comparison = self._comparison(q, metric, window)

        interpretation = None
        if metric == MetricKey.paid_pack_units and re.search(
            r"\brevenue\b|\bdollars?\b|\bsales in \$|\$", q
        ) and not context.wac_authorized:
            interpretation = (
                "Pricing is restricted at your access level, so this shows sales "
                "volume in packs rather than revenue."
            )

        return AnalyticalPlan(
            metric=metric, dimensions=dimensions, filters=filters,
            time=window, comparison=comparison, ranking=ranking,
            interpretation=interpretation,
        )

    # -- pieces --------------------------------------------------------------

    def _metric(self, q: str, context: PlanningContext) -> MetricKey:
        if re.search(r"market share|share of market|\bshare\b", q):
            if re.search(r"trend|chang\w+|grew|grown|declin\w+|movement", q):
                return MetricKey.share_trend_pp
            return MetricKey.brand_market_share
        if re.search(r"free drug|patient assistance|\bpap\b|hub dispense", q):
            if re.search(r"percent|proportion|share of total|% of", q):
                return MetricKey.pap_proportion
            return MetricKey.pap_volume
        if re.search(r"including free|total volume including", q):
            return MetricKey.total_volume_incl_free
        if re.search(r"market size|total market|market volume", q):
            return MetricKey.market_equivalents
        if re.search(r"how many (?:accounts|health systems)|account count|number of accounts", q):
            return MetricKey.account_count
        if re.search(r"how many facilit|facility count|number of facilit", q):
            return MetricKey.facility_count
        # "trend" alongside a period grain means a time series (volume plotted
        # per month), not a single growth figure. Only treat it as growth when
        # no period dimension was asked for.
        wants_series = bool(
            re.search(r"\bby month\b|\bmonthly\b|\bby quarter\b|\bquarterly\b|"
                      r"\bby week\b|\bweekly\b|\bover the last\b|\bover the past\b", q)
        )
        if re.search(r"grow\w*|grew|grown|declin\w*|increase|decrease|trend|trending", q) \
                and not re.search(r"market share", q):
            if not wants_series:
                return MetricKey.volume_growth
        if re.search(r"revenue|dollars?|\$|wac|gross sales", q):
            # Chosen only when permitted; otherwise volume, with the swap
            # disclosed. Authorization is still re-checked server-side.
            return MetricKey.wac_revenue if context.wac_authorized else MetricKey.paid_pack_units
        if re.search(r"equivalents?\b", q):
            return MetricKey.paid_equivalents
        return MetricKey.paid_pack_units

    def _dimensions(self, q: str, metric: MetricKey) -> list[Dimension]:
        dims: list[Dimension] = []
        # A trend request with no explicit grain still wants a series.
        if re.search(r"\btrend\b|\bover the last\b|\bover the past\b", q) and not re.search(
            r"\bby (?:month|quarter|week)\b|\bmonthly\b|\bquarterly\b|\bweekly\b", q
        ):
            if re.search(r"quarter", q):
                dims.append(Dimension.period_qtr)
            elif re.search(r"month|week", q):
                dims.append(Dimension.period_mo)
        for pattern, dim in self.DIMENSION_WORDS:
            if re.search(pattern, q) and dim not in dims:
                dims.append(dim)
            if len(dims) >= 2:
                break
        if metric in (MetricKey.account_count, MetricKey.facility_count):
            dims = [d for d in dims if d not in (Dimension.account, Dimension.facility)]
        return dims[:2]

    def _window(self, q: str, prev: dict[str, Any]):
        from app.analytics.plan import NamedWindow, TimeWindow

        if m := re.search(r"\b(20\d{2})[ -]?q([1-4])\b", q):
            return TimeWindow(kind="period_labels", period_labels=[f"{m.group(1)}-Q{m.group(2)}"])
        for pattern, name in self.WINDOWS:
            if re.search(pattern, q):
                return TimeWindow(kind="named", named=NamedWindow(name))
        if prev.get("time"):
            return TimeWindow.model_validate(prev["time"])
        return TimeWindow(kind="named", named=NamedWindow.r3m)

    def _comparison(self, q: str, metric: MetricKey, window):
        from app.analytics.plan import NamedWindow, TimeWindow

        needs = metric in (
            MetricKey.volume_growth, MetricKey.share_trend_pp, MetricKey.weighted_share_trend
        )
        if not needs:
            return None
        if m := re.search(r"\b(20\d{2})[ -]?q([1-4])\b", q):
            year = int(m.group(1))
            if re.search(r"year over year|vs last year|versus last year|yoy", q):
                return TimeWindow(kind="period_labels",
                                  period_labels=[f"{year - 1}-Q{m.group(2)}"])
        return TimeWindow(kind="named", named=NamedWindow.r6m_prior)

    def _filters(self, q: str, context: PlanningContext, prev: dict[str, Any]):
        from app.analytics.plan import Filters, TriState

        carried = prev.get("filters") or {}
        filters = Filters.model_validate(carried) if carried else Filters()
        update: dict[str, Any] = {}

        found = [p for p in context.known_products if re.search(rf"\b{re.escape(p.lower())}\b", q)]
        if found:
            update["product_names"] = found
        subs = [s for s in context.known_subcategories if s.lower() in q]
        if subs:
            update["market_subcategories"] = subs
        cats = [c for c in context.known_categories if c.lower() in q]
        if cats:
            update["market_categories"] = cats
        gpos = [g for g in context.known_gpos if g and g.lower() in q]
        if gpos:
            update["gpo_names"] = gpos
        terrs = [t for t in context.known_territories if t.lower() in q]
        if terrs:
            update["territories"] = terrs
        regions = [r for r in context.known_regions if r.lower() in q]
        if regions:
            update["regions"] = regions

        if re.search(r"exclude 340b|non-?340b|excluding 340b|without 340b", q):
            update["is_340b"] = TriState.exclude
        elif re.search(r"\b340b\b", q):
            update["is_340b"] = TriState.only
        if re.search(r"\bactive\b", q):
            update["active_only"] = True
        if re.search(r"standalone", q):
            update["standalone_only"] = True

        # "those accounts" freezes the previous cohort rather than re-ranking.
        if re.search(r"\bthose\b|\bthese\b|\bsame\b", q) and context.previous_cohort:
            update["account_ids"] = list(context.previous_cohort)

        return filters.model_copy(update=update) if update else filters

    def _ranking(self, q: str, dimensions: list[Dimension]):
        from app.analytics.plan import Ranking

        if not dimensions:
            return None
        if re.search(r"\bthose\b|\bthese\b|\bsame\b", q):
            return None            # a frozen cohort is not re-ranked
        direction = "bottom" if re.search(r"\bbottom\b|\blowest\b|\bworst\b", q) else "top"
        limit = 10
        if m := re.search(r"\btop\s+(\d{1,3})\b|\bbottom\s+(\d{1,3})\b", q):
            limit = int(m.group(1) or m.group(2))
        else:
            words = {"three": 3, "five": 5, "ten": 10, "twenty": 20}
            if m := re.search(r"\b(?:top|bottom)\s+(three|five|ten|twenty)\b", q):
                limit = words[m.group(1)]
            elif not re.search(r"\btop\b|\bbottom\b|\brank\b|\bhighest\b|\blowest\b", q):
                return None
        return Ranking(direction=direction, limit=min(limit, 500))


def build_planner(settings: Settings | None = None) -> Planner:
    settings = settings or get_settings()
    if settings.llm_provider == "bedrock":
        return BedrockPlanner(settings)
    return OfflinePlanner()
