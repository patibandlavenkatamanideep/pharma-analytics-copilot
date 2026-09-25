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


# Which filter a frozen cohort belongs in, per the grain it was collected at.
# A dimension that is not here (a period, for instance) has no cohort: "those
# same months" is a time window, not a population.
COHORT_FILTER_FIELD = {
    "account": "account_ids",
    "facility": "facility_ids",
    "product": "product_names",
    "gpo": "gpo_names",
    "archetype": "org_archetypes",
    "territory": "territories",
    "region": "regions",
}


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
    known_specialties: list[str] = field(default_factory=list)
    known_gpos: list[str] = field(default_factory=list)
    known_archetypes: list[str] = field(default_factory=list)
    known_territories: list[str] = field(default_factory=list)
    known_regions: list[str] = field(default_factory=list)
    # Recognition-only vocabularies (see entities.Vocabulary): naming a place
    # outside scope must produce a refusal, not a quietly re-scoped answer.
    all_territories: list[str] = field(default_factory=list)
    all_regions: list[str] = field(default_factory=list)
    previous_plan: dict[str, Any] | None = None
    previous_cohort: list[str] = field(default_factory=list)
    previous_cohort_dimension: str | None = None


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
        "- A bare 'volume', 'sales', 'demand' or 'units' means paid_pack_units.",
        "  Choose paid_equivalents ONLY when the user says 'equivalents' or asks",
        "  for a dose-normalised measure. Both are volume measures, but packs are",
        "  the default and equivalents must be asked for.",
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
    if context.known_specialties:
        parts += [
            f"Product therapeutic areas (filters.specialties): "
            f"{', '.join(context.known_specialties)}. "
            f"'our oncology portfolio' or 'urology products' means this filter, "
            f"NOT a market category and NOT the whole catalogue."
        ]
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
            "Carry forward everything the user did not change -- the metric, the "
            "dimensions, the filters, the time window AND THE RANKING. 'Break that "
            "down by X' adds a dimension and keeps the rest. 'Compare to last year' "
            "adds a comparison and keeps the population. 'Exclude 340B' sets "
            "filters.is_340b='exclude' and changes nothing else: if the previous plan "
            "ranked the top 5, the new plan still ranks the top 5. Drop the ranking "
            "only when the user asks for a frozen cohort ('those accounts') or asks "
            "to stop ranking.",
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
    """Bedrock adapter.

    Bedrock exposes Claude through two different endpoints and the model id
    decides which one serves it:

      * `AnthropicBedrockMantle` -- the Messages-API endpoint, which serves the
        newer unprefixed ids such as `anthropic.claude-opus-5`.
      * `AnthropicBedrock` -- the legacy bedrock-runtime InvokeModel path, which
        serves dated releases reached through a cross-region inference profile,
        e.g. `us.anthropic.claude-opus-4-5-20251101-v1:0`.

    Sending a dated id to Mantle returns a bare 404 "model does not exist",
    which reads like a permissions problem and is not one. The client is
    therefore chosen from the shape of the id rather than configured separately,
    so a model change cannot silently pick the wrong endpoint.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model_id = self.settings.bedrock_model_id
        self.last_usage: dict[str, int] = {}

        # A cross-region inference profile ("us." / "eu." / "global.") or a
        # dated, versioned id belongs to the legacy endpoint.
        legacy = (
            self.model_id.startswith(("us.", "eu.", "apac.", "global."))
            or ":" in self.model_id
        )
        if legacy:
            from anthropic import AnthropicBedrock as Client
        else:
            from anthropic import AnthropicBedrockMantle as Client
        self._legacy_endpoint = legacy

        self._client = Client(
            aws_region=self.settings.bedrock_region,
            timeout=self.settings.llm_timeout_s,
            max_retries=2,
        )
        log.info(
            "bedrock planner: model=%s endpoint=%s",
            self.model_id, "invoke-model" if legacy else "mantle",
        )

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
        request: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": self.settings.llm_max_tokens,
            "system": system,
            "messages": messages,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": "emit_plan"},
        }
        # Effort is a Messages-API field. The legacy InvokeModel endpoint
        # rejects unknown top-level fields, so it is only sent where it is
        # understood. Plan extraction is a constrained task either way.
        if not self._legacy_endpoint:
            request["output_config"] = {"effort": self.settings.llm_effort}

        response = self._client.messages.create(**request)
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
        (r"\bby territor\w*|\bper territor\w*|\b(?:all|each|every|compare)\s+territor\w*|\bterritories in\b", Dimension.territory),
        (r"\bby region\b|\bper region\b|\b(?:all|each|every|compare)\s+regions?\b", Dimension.region),
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
        # Deliberately NOT a bare \baccounts?\b: "excluding 340B accounts" is a
        # filter qualifier, not a request to break the answer down by account.
        (r"\bby account\b|\bper account\b|\beach account\b|\bby health system\b|"
         r"\bwhich accounts\b|\bwhich health systems\b|"
         # "does each health system have" is a request for a breakdown just as
         # much as "by health system" is.
         r"\b(?:each|every|per)\s+health\s+systems?\b|"
         r"\b(?:top|bottom|rank|ranked|list|biggest|largest|smallest)\b[^.?]*"
         r"\b(accounts?|health systems?)\b", Dimension.account),
    ]

    # Phrases that mean "change one thing about the last answer" rather than
    # "ask a new question".
    FOLLOW_UP = re.compile(
        r"^\s*(now|then|also|and|ok|okay)\b|"
        r"\bbreak (?:that|this|it) down\b|\bbreak down\b|"
        r"\bthose\b|\bthese\b|\bthat\b|\bsame\b|\binstead\b|"
        r"\bwhat about\b|\bhow about\b|\bexclude\b|\bonly\b",
        re.IGNORECASE,
    )

    def plan(self, question: str, context: PlanningContext) -> AnalyticalPlan:
        q = question.lower().strip()
        prev = context.previous_plan or {}

        # A follow-up PATCHES the previous plan. Recomputing it from scratch
        # would silently drop the population, the window and the ranking the
        # user is still talking about -- "break that down by quarter" must keep
        # the same accounts, not replace them with a single company total.
        is_follow_up = bool(prev) and bool(self.FOLLOW_UP.search(q))

        metric = self._metric(q, context)
        dimensions = self._dimensions(q, metric)
        window = self._window(q, prev if is_follow_up else {})
        # Filters carry over ONLY on a follow-up. A fresh question in the same
        # thread must not silently inherit an earlier "exclude 340B" and answer
        # something narrower than what was asked.
        filters = self._filters(q, context, prev if is_follow_up else {})
        dimensions = self._implied_dimensions(q, dimensions, filters)
        ranking = self._ranking(q, dimensions)
        comparison = self._comparison(q, metric, window)

        if is_follow_up:
            metric, dimensions, window, ranking, comparison = self._merge(
                q, prev, metric, dimensions, window, ranking, comparison, context
            )

        interpretation = None
        if is_follow_up:
            carried = []
            f = filters
            if f.is_340b.value != "include":
                carried.append(
                    "340B accounts excluded" if f.is_340b.value == "exclude"
                    else "340B accounts only"
                )
            if f.product_names:
                carried.append(", ".join(f.product_names))
            if f.gpo_names:
                carried.append(", ".join(f.gpo_names))
            if f.active_only:
                carried.append("active organizations only")
            if carried:
                interpretation = "Still applying: " + "; ".join(carried) + "."

        if metric == MetricKey.paid_pack_units and re.search(
            r"\brevenue\b|\bdollars?\b|\bsales in \$|\$", q
        ) and not context.wac_authorized:
            pricing_note = (
                "Pricing is restricted at your access level, so this shows sales "
                "volume in packs rather than revenue."
            )
            interpretation = (
                f"{pricing_note} {interpretation}" if interpretation else pricing_note
            )

        return AnalyticalPlan(
            metric=metric, dimensions=dimensions, filters=filters,
            time=window, comparison=comparison, ranking=ranking,
            interpretation=interpretation,
        )

    # -- pieces --------------------------------------------------------------

    def _merge(self, q, prev, metric, dimensions, window, ranking, comparison, context):
        """Apply only what the follow-up actually changed."""
        prev_dims = [Dimension(d) for d in prev.get("dimensions", [])]
        prev_metric = MetricKey(prev["metric"]) if prev.get("metric") else metric

        # Metric: keep the previous one unless this question names a different
        # one explicitly. A bare "exclude 340B" is not a metric change.
        names_metric = bool(
            re.search(
                r"market share|revenue|dollars?|\$|free drug|patient assistance|\bpap\b|"
                r"equivalents?|how many|count|growth|grew|declin",
                q,
            )
        )
        if not names_metric:
            metric = prev_metric

        # Dimensions: "break down by X" ADDS a grain to the existing one.
        added = [d for d in dimensions if d not in prev_dims]
        if re.search(r"\bbreak (?:that|this|it)? ?down\b|\bbreakdown\b|\balso by\b", q):
            dimensions = (prev_dims + added)[:2]
        elif added:
            # "by region instead" replaces; otherwise extend.
            dimensions = added[:2] if re.search(r"\binstead\b", q) else (prev_dims + added)[:2]
        else:
            dimensions = prev_dims

        # Window: keep the previous one unless this question names a period.
        if not self._names_window(q):
            prev_time = prev.get("time")
            if prev_time:
                from app.analytics.plan import TimeWindow
                window = TimeWindow.model_validate(prev_time)

        # Ranking: a follow-up that does not re-rank keeps the previous ranking,
        # so "exclude 340B" narrows the same top-5 question rather than
        # returning every account.
        if ranking is None and prev.get("ranking") and dimensions:
            from app.analytics.plan import Ranking
            ranking = Ranking.model_validate(prev["ranking"])

        # A frozen cohort is never re-ranked.
        if re.search(r"\bthose\b|\bthese\b|\bsame\b", q) and context.previous_cohort:
            ranking = None

        # Keep a comparison the previous plan had, if the metric still needs one.
        if comparison is None and prev.get("comparison"):
            from app.analytics.plan import TimeWindow
            comparison = TimeWindow.model_validate(prev["comparison"])

        return metric, dimensions, window, ranking, comparison

    def _names_window(self, q: str) -> bool:
        if re.search(r"\b(20\d{2})[ -]?q([1-4])\b", q):
            return True
        return any(re.search(pattern, q) for pattern, _ in self.WINDOWS)

    def _metric(self, q: str, context: PlanningContext) -> MetricKey:
        # A proportion question about 340B is a ratio, not a count. Answering
        # "what percentage of our volume comes from 340B accounts" with 59,419
        # packs is a different question.
        if re.search(r"\b340b\b", q) and re.search(
                r"percent|proportion|\bshare\b|fraction|% of|how much of", q):
            return MetricKey.share_340b
        if re.search(r"market share|share of market|\bshare\b", q):
            if re.search(
                r"trend|chang\w+|grew|grown|gain\w*|lost|losing|los\w*|"
                r"declin\w+|movement|improv\w+|versus the prior|vs the prior", q
            ):
                return MetricKey.share_trend_pp
            # "generic share" / "biosimilar share" is a share OF THE MARKET
            # held by a segment, not our share of it. Answering those with
            # brand_market_share returns the company's own share -- close to
            # the opposite of what was asked.
            if re.search(r"\bgeneric|\bbiosimilar|\bcompetitor", q, re.I):
                return MetricKey.market_segment_share
            return MetricKey.brand_market_share
        # Checked BEFORE the bare free-drug branch: "total volume including free
        # drug" contains "free drug" and would otherwise be read as PAP volume.
        if re.search(r"including free|total volume including|paid and free", q):
            return MetricKey.total_volume_incl_free
        if re.search(r"free drug|patient assistance|\bpap\b|hub dispense", q):
            if re.search(r"percent|proportion|share of total|% of", q):
                return MetricKey.pap_proportion
            return MetricKey.pap_volume
        if re.search(r"market size|total market|market volume", q):
            return MetricKey.market_equivalents
        # Counting questions are not always phrased as "how many". The supplied
        # documents ask "Which health systems have the most facilities?", which
        # matched neither pattern and fell through to volume -- a ranking of
        # health systems by PACK UNITS, presented as an answer about facility
        # counts.
        #
        # The facility form is checked first: "which health systems have the
        # most facilities" names both entities, and the one being COUNTED is
        # the one the superlative governs.
        # Structural first: "all facilities", "facilities we have", "on
        # record" are about the hierarchy, not about who transacted. The
        # sales-derived count silently excluded 14,439 of 40,000 facilities.
        if re.search(
            r"\ball facilit\w+|\btotal facilit\w+|facilit\w+ on record|"
            r"facilit\w+ (?:do|does) (?:we|they|it) have|"
            r"how many facilit\w+ (?:do|does) (?:we|they)\b", q
        ):
            return MetricKey.facility_count_all
        if re.search(
            r"how many[^?]*\bfacilit\w+|facility count|"
            r"number of (?:distinct |active )?facilit\w+|"
            r"(?:most|fewest|highest number of|largest number of|"
            r"greatest number of)\s+(?:\w+\s+){0,2}facilit\w+", q
        ):
            return MetricKey.facility_count
        if re.search(
            r"how many[^?]*\b(accounts|health systems|systems|idns)\b|"
            r"account count|number of (?:distinct )?accounts|count of accounts|"
            r"(?:most|fewest|highest number of|largest number of)\s+"
            r"(?:\w+\s+){0,2}(?:accounts|health systems|idns)\b", q
        ):
            return MetricKey.account_count
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
        # A count must not be grouped by the thing it counts -- "accounts per
        # account" is not a question. Grouping by the OTHER entity is exactly
        # the question, though: "which health systems have the most facilities"
        # counts facilities per account. Stripping both left that question with
        # no breakdown at all, so it returned one company-wide number.
        if metric is MetricKey.account_count:
            dims = [d for d in dims if d is not Dimension.account]
        elif metric is MetricKey.facility_count:
            dims = [d for d in dims if d is not Dimension.facility]
        return dims[:2]

    def _implied_dimensions(self, q: str, dims, filters):
        """Grains implied by what the question compares rather than names."""
        out = list(dims)
        comparing = bool(re.search(r"\bcompare\b|\bvs\.?\b|\bversus\b", q))
        if comparing and len(filters.gpo_names) > 1 and Dimension.gpo not in out:
            out.insert(0, Dimension.gpo)
        if comparing and len(filters.org_archetypes) > 1 and Dimension.archetype not in out:
            out.insert(0, Dimension.archetype)
        if comparing and len(filters.product_names) > 1 and Dimension.product not in out:
            out.insert(0, Dimension.product)
        return out[:2]

    def _window(self, q: str, prev: dict[str, Any]):
        from app.analytics.plan import NamedWindow, TimeWindow

        # Both orderings appear in practice: "2026 Q1" and "Q1 2026".
        labels = [f"{y}-Q{n}" for y, n in re.findall(r"\b(20\d{2})[ -]?q([1-4])\b", q)]
        labels += [f"{y}-Q{n}" for n, y in re.findall(r"\bq([1-4])[ -](20\d{2})\b", q)]
        if labels:
            return TimeWindow(kind="period_labels", period_labels=sorted(set(labels)))
        for pattern, name in self.WINDOWS:
            if re.search(pattern, q):
                return TimeWindow(kind="named", named=NamedWindow(name))
        # Only a follow-up inherits the previous window; `prev` is empty
        # otherwise, so a new question defaults to R3M and says so.
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

        # Every vocabulary match is anchored on word boundaries. Plain substring
        # matching silently produced wrong answers: the GPO "ION" matches inside
        # "reg-ION", so "territories in my region" was filtered to ION-affiliated
        # accounts only, quietly cutting a Director's totals by ~80%.
        def mentioned(values: list[str]) -> list[str]:
            return [v for v in values if v and re.search(rf"\b{re.escape(v.lower())}\b", q)]

        if found := mentioned(context.known_products):
            update["product_names"] = found
        if subs := mentioned(context.known_subcategories):
            update["market_subcategories"] = subs
        if cats := mentioned(context.known_categories):
            update["market_categories"] = cats
        # "our oncology portfolio" is a filter on products.specialty. Without
        # this the phrase resolved to nothing and the answer was every
        # product's volume, presented as the oncology figure.
        if specs := mentioned(context.known_specialties):
            update["specialties"] = specs
        if gpos := mentioned(context.known_gpos):
            update["gpo_names"] = gpos
        if archetypes := mentioned(context.known_archetypes):
            update["org_archetypes"] = archetypes
        if terrs := mentioned(context.all_territories or context.known_territories):
            update["territories"] = terrs
        if regions := mentioned(context.all_regions or context.known_regions):
            update["regions"] = regions

        if re.search(r"exclude 340b|non-?340b|excluding 340b|without 340b", q):
            update["is_340b"] = TriState.exclude
        elif re.search(r"\b340b\b", q):
            update["is_340b"] = TriState.only
        if re.search(r"\bactive\b", q):
            update["active_only"] = True
        if re.search(r"standalone", q):
            update["standalone_only"] = True

        # The segment the share is OF. Without this the numerator and the
        # denominator are the same population and every answer is 100%.
        segments = [
            value for word, value in (
                (r"\bgenerics?\b", "generic"),
                (r"\bbiosimilars?\b", "biosimilar"),
                (r"\bbranded competitors?\b|\bcompetitors?\b", "branded_competitor"),
            ) if re.search(word, q, re.I)
        ]
        if segments and not filters.classifications:
            update["classifications"] = segments

        # "those accounts" freezes the previous cohort rather than re-ranking.
        #
        # The cohort goes into the filter that matches the grain it was
        # collected at. It used to go into account_ids whatever it held, so a
        # cohort of products became a list of organization ids and matched
        # nothing -- a follow-up that looked like it worked and returned an
        # empty or wrong population.
        if re.search(r"\bthose\b|\bthese\b|\bsame\b", q) and context.previous_cohort:
            target = COHORT_FILTER_FIELD.get(context.previous_cohort_dimension or "")
            if target:
                update[target] = list(context.previous_cohort)

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
