"""Compile a validated plan into SQL.

Guarantees this module is responsible for:

  * Every identifier (table, column, expression) comes from a closed allowlist
    in this file. Nothing is ever interpolated from a plan, a question or a
    model response -- parameterization cannot protect an identifier, so
    identifiers are simply never variable.
  * Every value travels as a bound parameter.
  * Ratios are aggregated as SUM(numerator)/SUM(denominator), never as the mean
    of per-row percentages, and the two sources are pre-aggregated to a common
    grain in separate CTEs before being joined, so a many-to-many raw-sales join
    cannot multiply rows.
  * Scope filters are NOT emitted here. Row scope is enforced by RLS from
    server-derived context; emitting it in SQL too would invite the mistake of
    treating the SQL as the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.analytics.periods import ResolvedWindow, resolve
from app.analytics.plan import AnalyticalPlan, Dimension, Filters, MetricKey, TriState
from app.analytics.registry import MetricRegistry, get_registry
from app.analytics.thresholds import SQL_OPERATOR


class CompileError(ValueError):
    pass


class UnsupportedCombination(CompileError):
    """The plan asks for something this metric cannot express.

    Distinct from a compile failure: nothing is broken, the question just
    combines things that have no defined meaning together -- a structural
    count broken down by month, a comparison window on a metric that reports
    one window. The caller should ask the user to rephrase, not report an
    error. `reasons` holds one user-meaningful sentence per incompatibility.
    """

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__(" ".join(self.reasons))


@dataclass(frozen=True)
class CohortBinding:
    """A previous answer's population, frozen into this query by the SERVER.

    The planner cannot express it: the typed plan's account filter holds at
    most 200 ids, and a 500-account answer followed by "those same accounts"
    must mean all 500. The ids come from the conversation's stored cohort,
    read under the caller's current access -- never from the model -- and are
    applied through exactly the expression the typed filter for that grain
    uses, so membership means the same thing it always did.
    """
    dimension: str
    ids: tuple[str, ...]


#: The typed filter a cohort of each grain stands in for, and its expression.
_COHORT_EXPRESSION: dict[str, tuple[str, tuple[str, ...], bool]] = {
    # grain: (expression, joins needed, organisation-side)
    "account": ("COALESCE(o.grandparent_org_id, o.org_id)", ("o",), True),
    "facility": ("o.org_id", ("o",), True),
    "gpo": ("o.gpo_name", ("o",), True),
    "archetype": ("o.org_archetype", ("o",), True),
    "territory": ("z.territory_name", ("o", "z"), True),
    "region": ("z.region_name", ("o", "z"), True),
    "product": ("upper(p.drug_name)", ("p",), False),
}


# --- identifier allowlists ---------------------------------------------------
# key -> (id expression, label expression, tables required)

@dataclass(frozen=True)
class DimSpec:
    id_expr: str
    label_expr: str
    needs: tuple[str, ...]
    description: str


DIMENSIONS: dict[Dimension, DimSpec] = {
    # A7: identity is the id; the name is only a label, because 267 name groups
    # are shared by more than one org_id in the full dataset.
    Dimension.account: DimSpec(
        "COALESCE(o.grandparent_org_id, o.org_id)",
        "COALESCE(o.grandparent_org_name, o.org_name)",
        ("o",), "top-level account (grandparent, or the facility if standalone)"),
    Dimension.parent: DimSpec(
        "COALESCE(o.parent_org_id, o.org_id)",
        "COALESCE(o.parent_org_name, o.org_name)",
        ("o",), "parent organization"),
    Dimension.facility: DimSpec("o.org_id", "o.org_name", ("o",), "individual facility"),
    Dimension.territory: DimSpec("z.territory_name", "z.territory_name", ("o", "z"), "sales territory"),
    Dimension.region: DimSpec("z.region_name", "z.region_name", ("o", "z"), "sales region"),
    Dimension.state: DimSpec("o.state", "o.state", ("o",), "state"),
    Dimension.product: DimSpec("p.drug_name", "p.drug_name", ("p",), "product"),
    Dimension.ndc: DimSpec("p.ndc", "p.ndc", ("p",), "NDC"),
    Dimension.strength: DimSpec("p.strength", "p.strength", ("p",), "strength"),
    Dimension.form: DimSpec("p.form", "p.form", ("p",), "formulation"),
    Dimension.market_category: DimSpec("p.market_category", "p.market_category", ("p",), "market category"),
    Dimension.market_subcategory: DimSpec("p.market_subcategory", "p.market_subcategory", ("p",), "market subcategory"),
    Dimension.specialty: DimSpec("p.specialty", "p.specialty", ("p",), "specialty"),
    Dimension.gpo: DimSpec("o.gpo_name", "o.gpo_name", ("o",), "GPO affiliation"),
    Dimension.archetype: DimSpec("o.org_archetype", "o.org_archetype", ("o",), "organization archetype"),
    Dimension.is_340b: DimSpec("o.is_340b", "o.is_340b", ("o",), "340B status"),
    Dimension.org_status: DimSpec("o.org_status", "o.org_status", ("o",), "organization status"),
    Dimension.data_source: DimSpec("s.data_source", "s.data_source", (), "data source"),
    Dimension.classification: DimSpec("c.classification", "c.classification", ("p", "c"), "derived product classification"),
    Dimension.period_mo: DimSpec("s.period_mo", "s.period_mo", (), "reporting month"),
    Dimension.period_qtr: DimSpec("s.period_qtr", "s.period_qtr", (), "reporting quarter"),
    Dimension.period_wk: DimSpec("s.period_wk", "s.period_wk", (), "reporting week"),
}

# The market denominator is defined at market_subcategory level. Grouping it by
# our own drug name would collapse "the market" to our own product and make every
# share either 100% or NULL -- exactly the mistake docs/ASSUMPTIONS.md#a3 warns
# about. So a product-level grain on the numerator is BRIDGED to subcategory on
# the denominator: ZENOVAX's share is measured against the Docetaxel market.
PRODUCT_GRAINS = {
    Dimension.product, Dimension.ndc, Dimension.strength,
    Dimension.form, Dimension.classification,
}
# Grains the market data shares natively, usable on both sides unchanged.
BRIDGE_DIMENSION = Dimension.market_subcategory

JOINS = {
    "o": "JOIN organizations o ON o.org_id = s.org_id",
    "p": "JOIN products p ON p.ndc = s.ndc",
    "z": "LEFT JOIN zip_territory z ON z.zip = o.zip",
    "c": "LEFT JOIN app_ref.product_classification c ON c.ndc = p.ndc",
}

#: The relations a query can reach from each base table. Almost every metric
#: reads `sales s` and joins outwards. A structural count reads the
#: organization hierarchy directly, so `organizations o` IS the base: it must
#: not be joined in again, geography is reachable through o.zip, and nothing
#: product- or transaction-side is reachable at all -- products join through
#: sales.ndc, and periods and data source are sales columns.
REACHABLE_FROM: dict[str, frozenset[str]] = {
    "sales": frozenset({"o", "p", "z", "c"}),
    "organizations": frozenset({"o", "z"}),
}

#: Filters that constrain the product side. Meaningless for a query that
#: never touches a product.
PRODUCT_FILTERS = ("product_names", "ndcs", "strengths", "market_categories",
                   "market_subcategories", "specialties", "classifications")


#: The calendar column each period grain is labelled by.
PERIOD_COLUMN: dict[Dimension, str] = {
    Dimension.period_mo: "period_mo",
    Dimension.period_qtr: "period_qtr",
    Dimension.period_wk: "period_wk",
}


#: A period grain as a word, for messages.
GRAIN_WORD = {
    Dimension.period_mo: "month",
    Dimension.period_qtr: "quarter",
    Dimension.period_wk: "week",
}


def needs_dense_series(plan: AnalyticalPlan) -> bool:
    """Should this answer include the periods that have no rows?

    A rolling average always does: averaging the rows that happen to exist
    averaged June with September when July and August were empty. A single
    time series ("volume by month" for one product) does too: a series that
    silently skips months reads as a different trend. A breakdown by period
    AND something else does not, because zero-filling the cross product would
    add a row for every account in every month -- the note says periods with
    no rows are omitted instead.

    A window given as calendar dates is excluded: reporting periods follow the
    week-ending month, so dates do not select whole periods.
    """
    periods = [d for d in plan.dimensions if d in PERIOD_COLUMN]
    if len(periods) != 1 or plan.time.kind == "date_range":
        return False
    # A period-over-period change does too, even beside another dimension: a
    # month with no rows for a group is that group's zero (or unknown), and
    # skipping it would compare September with July and call it "the month
    # before".
    return plan.rolling is not None or plan.period_over_period or len(plan.dimensions) == 1


def _reads_sales(ds: "DimSpec") -> bool:
    return "s." in ds.id_expr or "s." in ds.label_expr


def reachable(dim: Dimension, base: str) -> bool:
    """Can this grain be computed from `base` without inventing a join?"""
    ds = DIMENSIONS[dim]
    if base == "organizations" and _reads_sales(ds):
        return False
    return set(ds.needs) <= REACHABLE_FROM[base]


def check_compatibility(plan: AnalyticalPlan, spec: dict[str, Any]) -> list[str]:
    """Every reason this plan cannot be answered as asked, or [] if it can.

    One place, so the answer to "can this metric be broken down by that" does
    not depend on which code path happens to notice first. Before this
    existed, three different things happened to three unsupported plans: a
    structural count by territory failed in PostgreSQL with DuplicateAlias; a
    structural count by month failed with a missing FROM entry; and a
    comparison window on a single-window metric was silently dropped, so
    "share this quarter compared with last" came back as one quarter.
    """
    kind = spec.get("kind")
    reasons: list[str] = []

    if plan.comparison is not None and kind != "period_change":
        reasons.append(
            f"{spec['label'].capitalize()} reports one window, so a comparison "
            f"with another period cannot be shown with it. Growth "
            f"(volume_growth) and share change (share_trend_pp) compare two "
            f"windows."
        )

    if kind == "period_change":
        # A period grain inside a two-window comparison cannot be joined. The
        # current side is labelled 2026-Q3 and the prior side 2026-Q2, so the
        # FULL JOIN matches nothing: every row comes back with one side null
        # and a null growth figure -- which looks like "no growth data" rather
        # than like a question the system cannot express. Aligning by
        # position would be a guess about what was meant; a growth figure for
        # each period against its own prior is a different query shape.
        for d in plan.dimensions:
            if d in (Dimension.period_mo, Dimension.period_qtr, Dimension.period_wk):
                # The grain as a word: the message used to say "a mo-by-mo
                # breakdown" and "volume by mo".
                grain = GRAIN_WORD[d]
                reasons.append(
                    f"A {grain}-by-{grain} breakdown cannot also be a two-window "
                    f"comparison: each side would be labelled with a different "
                    f"{grain}, so nothing lines up. Ask for the trend "
                    f"(\"volume by {grain}\") to see the series, \"{grain} over "
                    f"{grain}\" to see each {grain} against the one before it, or "
                    f"drop the {grain} breakdown to see one growth figure for the window."
                )
                break

    if plan.period_over_period:
        if kind not in (None, "count_distinct"):
            reasons.append(
                f"Each period against the one before it (period over period) is "
                f"computed for a volume or count that adds up over periods. "
                f"{spec['label'].capitalize()} does not: a share or a growth figure "
                f"is not added across periods, and a structural count has no "
                f"series. Ask for {spec['label']} by period to see how it moves."
            )
        if plan.time.kind == "date_range":
            reasons.append(
                "A period-over-period change steps through reporting periods, and a "
                "window given as calendar dates does not select whole periods. Asking "
                "for a number of months or weeks gives the same change over whole periods."
            )

    if plan.rolling is not None and plan.time.kind == "date_range":
        reasons.append(
            "A rolling average is taken over reporting periods, and a window "
            "given as calendar dates does not select whole periods: reporting "
            "months follow the week-ending date. Asking for a number of months "
            "or weeks gives the same average over whole periods."
        )

    if kind == "count_structural":
        entity = spec.get("count_entity", "organization")
        bad_dims = [d.value for d in plan.dimensions
                    if not reachable(d, "organizations")]
        if bad_dims:
            reasons.append(
                f"A count of {entity} records comes from the organization "
                f"hierarchy, which has no {', '.join(bad_dims)} -- those "
                f"belong to sales. The {entity}-with-sales count can be broken "
                f"down that way."
            )
        bad_filters = [f for f in PRODUCT_FILTERS if getattr(plan.filters, f)]
        if bad_filters:
            reasons.append(
                f"A count of {entity} records is not tied to any product, so "
                f"it cannot be restricted by {', '.join(bad_filters)}. The "
                f"{entity}-with-sales count can."
            )
        if plan.rolling is not None:
            reasons.append(
                "A count of records on file has no series over time to "
                "average."
            )

    return reasons


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]
    columns: list[str]
    unit: str
    metric_label: str
    window_label: str
    comparison_label: str | None = None
    # The period still accumulating, when the window includes it: its total
    # and any change to it are provisional.
    current_period: str | None = None
    notes: list[str] = field(default_factory=list)
    quality_checks: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        import hashlib
        return hashlib.sha256(self.sql.encode()).hexdigest()[:16]


# PostgreSQL supports FULL OUTER JOIN only on merge- or hash-joinable
# conditions, which rules out `IS NOT DISTINCT FROM`. Coalescing each key to a
# sentinel keeps plain equality (hash-joinable) while still matching NULL to
# NULL, which matters because a dimension value can legitimately be NULL (an
# organization with no GPO, a facility with no parent).
NULL_KEY_SENTINEL = "\x01__NULL__"


def _join_key(alias: str, column: str) -> str:
    return f"COALESCE({alias}.{column}::text, %s)"


class Compiler:
    def __init__(self, registry: MetricRegistry | None = None, max_rows: int = 5000) -> None:
        self.registry = registry or get_registry()
        self.max_rows = max_rows

    # -- filter construction -------------------------------------------------

    def _business_filters(
        self, filters: Filters, needs: set[str], *, org_side: bool = True
    ) -> tuple[list[str], list[Any]]:
        """Filters that apply identically to every component of a metric.

        Geography values here narrow WITHIN the authorized scope; they are not
        the authorization itself. The caller has already checked them against
        the principal's scope.
        """
        clauses: list[str] = []
        params: list[Any] = []

        def add_in(expr: str, values: list[Any], need: tuple[str, ...]) -> None:
            if values:
                needs.update(need)
                clauses.append(f"{expr} = ANY(%s)")
                params.append(list(values))

        add_in("upper(p.drug_name)", [v.upper() for v in filters.product_names], ("p",))
        add_in("p.ndc", filters.ndcs, ("p",))
        add_in("upper(p.strength)", [v.upper() for v in filters.strengths], ("p",))
        add_in("p.market_category", filters.market_categories, ("p",))
        add_in("p.market_subcategory", filters.market_subcategories, ("p",))
        add_in("p.specialty", filters.specialties, ("p",))
        add_in("c.classification", filters.classifications, ("p", "c"))

        if org_side:
            add_in("COALESCE(o.grandparent_org_id, o.org_id)", filters.account_ids, ("o",))
            add_in("o.org_id", filters.facility_ids, ("o",))
            add_in("o.org_archetype", filters.org_archetypes, ("o",))
            add_in("o.gpo_name", filters.gpo_names, ("o",))
            add_in("o.org_type", filters.org_types, ("o",))
            add_in("o.state", filters.states, ("o",))
            add_in("z.territory_name", filters.territories, ("o", "z"))
            add_in("z.region_name", filters.regions, ("o", "z"))

            # A10: 340B is an organization attribute, so this filters facility
            # CONTRIBUTIONS, not whole health systems.
            if filters.is_340b is TriState.exclude:
                needs.add("o")
                clauses.append("COALESCE(o.is_340b, 0) = 0")
            elif filters.is_340b is TriState.only:
                needs.add("o")
                clauses.append("COALESCE(o.is_340b, 0) = 1")

            if filters.active_only:
                needs.add("o")
                clauses.append("o.org_status = %s")
                params.append("Active")
            if filters.standalone_only:
                needs.add("o")
                clauses.append("o.grandparent_org_id IS NULL AND o.parent_org_id IS NULL")

        # A frozen cohort, applied where its grain's typed filter would be:
        # organisation-side grains only on the organisation side, exactly as
        # account_ids is, so a ratio's widened denominator stays widened.
        if self._cohort is not None:
            expr, need, org = _COHORT_EXPRESSION[self._cohort.dimension]
            if org_side or not org:
                ids = ([i.upper() for i in self._cohort.ids]
                       if self._cohort.dimension == "product" else list(self._cohort.ids))
                if ids:
                    add_in(expr, ids, need)
                else:
                    # add_in skips an empty list -- right for an optional
                    # filter, wrong here: "those" over an empty answer is
                    # nobody, not everybody.
                    clauses.append("FALSE")

        return clauses, params

    def _source_filters(self, spec: dict[str, Any], needs: set[str]) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if sources := spec.get("sources"):
            clauses.append("s.data_source = ANY(%s)")
            params.append(list(sources))
        if spec.get("company_only"):
            clauses.append("s.brand_flag = 1")
        return clauses, params

    def _component_sql(self, spec: dict[str, Any], needs: set[str]) -> str:
        component = spec["component"]
        expr = self.registry.component_expr(component)
        if "p." in expr:
            needs.add("p")
        return expr

    # -- leaf aggregate ------------------------------------------------------

    def _leaf_select(
        self,
        metric_key: str,
        filters: Filters,
        window: ResolvedWindow,
        *,
        dims: list[Dimension],
        join_dims: list[Dimension] | None = None,
        extra_clauses: list[str] | None = None,
        extra_params: list[Any] | None = None,
        org_side: bool = True,
    ) -> tuple[str, list[Any], set[str]]:
        """One pre-aggregated component.

        `dims` are the grains reported back to the user (dim0_id/dim0_label...).
        `join_dims` are additional grains projected as jk0, jk1... purely so two
        components can be joined at a common grain -- used when a product-level
        numerator has to meet a subcategory-level market denominator.
        """
        spec = self.registry.get(metric_key)
        needs: set[str] = set()
        params: list[Any] = []

        select_parts: list[str] = []
        group_parts: list[str] = []
        for i, dim in enumerate(dims):
            ds = DIMENSIONS[dim]
            needs.update(ds.needs)
            select_parts.append(f"{ds.id_expr} AS dim{i}_id")
            select_parts.append(f"{ds.label_expr} AS dim{i}_label")
            group_parts.append(ds.id_expr)
            group_parts.append(ds.label_expr)
        for j, dim in enumerate(join_dims or []):
            ds = DIMENSIONS[dim]
            needs.update(ds.needs)
            select_parts.append(f"{ds.id_expr} AS jk{j}")
            group_parts.append(ds.id_expr)

        kind = spec.get("kind")
        if kind in ("count_distinct", "count_structural"):
            needs.add("o")
            entity = spec["count_entity"]
            target = (
                "COALESCE(o.grandparent_org_id, o.org_id)" if entity == "account" else "o.org_id"
            )
            value_expr = f"count(DISTINCT {target})"
            if kind == "count_structural" and entity == "facility":
                # org_hierarchy.md defines a facility as org_type = 'Facility'.
                # organizations holds all three levels -- 37,500 facilities,
                # 2,000 parents, 500 grandparents -- so counting every row
                # reported 40,000 facilities, 2,500 of which are not
                # facilities. The sales-derived count never hit this because
                # sales.org_id is always a facility.
                extra_structural = "o.org_type = 'Facility'"
            else:
                extra_structural = None
        else:
            value_expr = f"{spec['aggregate']}({self._component_sql(spec, needs)})"
        select_parts.append(f"{value_expr} AS value")

        where: list[str] = []
        src_clauses, src_params = self._source_filters(spec, needs)
        where += src_clauses
        params += src_params

        # A structural count is a property of the hierarchy, not of a period:
        # a facility does not stop existing because it had no sales last
        # quarter. The window clause also references the sales table, which
        # this query does not read.
        if kind != "count_structural":
            where.append(window.sql)
            params += window.params

        biz_clauses, biz_params = self._business_filters(filters, needs, org_side=org_side)
        where += biz_clauses
        params += biz_params

        if kind == "count_structural" and extra_structural:
            where.append(extra_structural)

        if extra_clauses:
            where += extra_clauses
            params += extra_params or []

        # A structural count is about the hierarchy, not about transactions,
        # so it reads organizations directly. Row-level security still applies
        # -- organizations carries the same policy as sales -- so scope is
        # enforced exactly as it is everywhere else.
        base = "organizations" if kind == "count_structural" else "sales"
        # The joins are rendered here rather than by each caller, because
        # only this function knows which relation is the base. Rendered by the
        # callers, a structural count joined organizations to itself.
        sql = (
            f"SELECT {', '.join(select_parts)}\n"
            f"FROM {'organizations o' if base == 'organizations' else 'sales s'}\n"
            f"{self._render_joins(needs, base)}\n"
            f"WHERE {' AND '.join(where) if where else 'TRUE'}\n"
        )
        if group_parts:
            sql += f"GROUP BY {', '.join(group_parts)}\n"
        return sql, params, needs

    def _render_joins(self, needs: set[str], base: str = "sales") -> str:
        # z depends on o; c depends on p. Order is fixed, not data-dependent.
        needs = set(needs)
        if "z" in needs:
            needs.add("o")
        if "c" in needs:
            needs.add("p")
        unreachable = needs - REACHABLE_FROM[base]
        if unreachable:
            # check_compatibility should have refused the plan already; this
            # is the backstop that keeps a missed case from reaching SQL.
            raise CompileError(
                f"internal: {sorted(unreachable)} not reachable from {base}")
        if base == "organizations":
            # organizations is the FROM table, so joining it again is the
            # DuplicateAlias this used to produce whenever geography was
            # asked for: `z` pulled `o` back in after the structural branch
            # had discarded it.
            needs.discard("o")
        return "\n".join(JOINS[k] for k in ("o", "p", "z", "c") if k in needs)

    # -- public entry point --------------------------------------------------

    #: Set only on a per-call copy made by compile(); never on a shared
    #: instance, which serves concurrent requests.
    _cohort: CohortBinding | None = None

    def compile(
        self,
        plan: AnalyticalPlan,
        *,
        anchor: dict[str, Any],
        cohort: CohortBinding | None = None,
    ) -> CompiledQuery:
        if cohort is not None:
            if cohort.dimension not in _COHORT_EXPRESSION:
                raise CompileError(f"no filter for a cohort of {cohort.dimension}")
            # A copy for this call: the binding is request state, and one
            # Compiler serves every request in the process.
            import copy
            bound = copy.copy(self)
            bound._cohort = cohort
            return bound._compile(plan, anchor=anchor)
        return self._compile(plan, anchor=anchor)

    def _compile(
        self,
        plan: AnalyticalPlan,
        *,
        anchor: dict[str, Any],
    ) -> CompiledQuery:
        spec = self.registry.get(plan.metric.value)
        kind = spec.get("kind")
        if reasons := check_compatibility(plan, spec):
            raise UnsupportedCombination(reasons)
        window = resolve(plan.time, anchor)
        notes: list[str] = list(window.caveats)
        if window.incomplete_period:
            notes.append(
                "This window includes the current reporting period, which is still "
                "accumulating."
            )

        if needs_dense_series(plan):
            query = self._compile_dense_series(plan, spec, window, anchor)
        elif kind == "ratio":
            query = self._compile_ratio(plan, spec, window, anchor)
        elif kind == "period_change":
            query = self._compile_change(plan, spec, window, anchor)
        else:
            query = self._compile_simple(plan, spec, window)

        query.notes = notes + query.notes
        query.window_label = window.label
        if any(d in PERIOD_COLUMN for d in plan.dimensions) and not needs_dense_series(plan):
            query.notes.append(
                "Periods with no rows for a group are not listed; a missing "
                "period means none, not unknown.")
        if spec.get("kind") == "count_structural":
            # Otherwise the answer carries a reporting window it did not use.
            query.window_label = "all periods (a structural count)"
            query.notes.append(
                "This counts facilities on record in the organization hierarchy, "
                "whether or not they had sales in any period."
            )
        if spec.get("proposed"):
            query.notes.append(
                f"{spec['label']} uses a proposed interpretation: the supplied definition "
                "does not state which volume weights the trend. Total market equivalents "
                "is used here."
            )
        return query

    def _finish(self, sql: str, plan: AnalyticalPlan, params: list[Any],
                value_col: str = "value", *,
                rolled: bool = False) -> tuple[str, list[Any]]:
        """Apply the threshold, then order and limit.

        The threshold wraps the whole body rather than becoming a HAVING
        clause, because the value is computed differently in each shape -- an
        aggregate for a simple metric, an outer expression for a ratio or a
        comparison. Wrapping works for all three and keeps the predicate in
        one place.

        Order matters: filter first, then rank and cap. A display cap applied
        before the filter would rank the wrong population.
        """
        # The rolling average wraps first, so a threshold filters the
        # AVERAGED value -- "months where the rolling average fell below X"
        # is about the average, not about the raw point.
        if plan.rolling is not None and not rolled:
            period_index = next(
                i for i, d in enumerate(plan.dimensions)
                if d in (Dimension.period_mo, Dimension.period_qtr,
                         Dimension.period_wk)
            )
            order_col = f"dim{period_index}_id"
            partition = [
                f"dim{i}_id" for i in range(len(plan.dimensions))
                if i != period_index
            ]
            over = (
                (f"PARTITION BY {', '.join(partition)} " if partition else "")
                + f"ORDER BY {order_col} "
                f"ROWS BETWEEN {plan.rolling.periods - 1} PRECEDING AND CURRENT ROW"
            )
            carried = ", ".join(
                f"dim{i}_id, dim{i}_label" for i in range(len(plan.dimensions)))
            sql = (
                f"SELECT {carried}, {value_col} AS point_value,\n"
                f"       AVG({value_col}) OVER ({over}) AS {value_col}\n"
                f"FROM (\n{sql}) AS series\n"
            )

        if plan.threshold is not None:
            operator = SQL_OPERATOR[plan.threshold.op]
            sql = (
                f"SELECT * FROM (\n{sql}) AS filtered\n"
                f"WHERE {value_col} IS NOT NULL AND {value_col} {operator} %s\n"
            )
            params = params + [plan.threshold.value]
        return sql + self._order_and_limit(plan, value_col), params

    def _order_and_limit(self, plan: AnalyticalPlan, value_col: str = "value") -> str:
        out = ""
        if plan.ranking:
            direction = "DESC" if plan.ranking.direction == "top" else "ASC"
            out += f"ORDER BY {value_col} {direction} NULLS LAST\n"
            out += f"LIMIT {int(plan.ranking.limit)}\n"
        elif plan.period_over_period:
            period_index = next(i for i, d in enumerate(plan.dimensions)
                                if d in PERIOD_COLUMN)
            keys = [f"dim{i}_label" for i in range(len(plan.dimensions)) if i != period_index]
            out += f"ORDER BY {', '.join(keys + [f'dim{period_index}_id'])} ASC\n"
            out += f"LIMIT {int(self.max_rows) + 1}\n"
        elif plan.dimensions:
            first = plan.dimensions[0]
            # Period dimensions read naturally in chronological order.
            if first in (Dimension.period_mo, Dimension.period_qtr, Dimension.period_wk):
                out += "ORDER BY dim0_id ASC\n"
            else:
                out += f"ORDER BY {value_col} DESC NULLS LAST\n"
            # One more than the cap, so the renderer can tell a result that
            # exactly fills the cap from one that was cut short. With LIMIT
            # equal to the cap the two are indistinguishable and truncation is
            # silently reported as a complete answer.
            out += f"LIMIT {int(self.max_rows) + 1}\n"
        return out

    def _columns(self, plan: AnalyticalPlan, extra: list[str] | None = None) -> list[str]:
        cols: list[str] = []
        for i, dim in enumerate(plan.dimensions):
            cols += [f"dim{i}_id", f"dim{i}_label"]
        if plan.rolling is not None:
            # The un-averaged point is kept beside the average: a rolling
            # figure is hard to sanity-check without the series it came from.
            cols += ["point_value"]
        if plan.period_over_period:
            cols += ["prior_value", "change", "change_pct", "weeks", "prior_weeks"]
        cols += extra or ["value"]
        return cols

    def _sources_of(self, spec: dict[str, Any]) -> list[str]:
        """Every data source a metric reads, through ratio components."""
        found: set[str] = set(spec.get("sources") or [])
        for side in ("numerator", "denominator"):
            if key := spec.get(side):
                found |= set(self._sources_of(self.registry.get(key)))
        return sorted(found)

    def _compile_dense_series(
        self,
        plan: AnalyticalPlan,
        spec: dict[str, Any],
        window: ResolvedWindow,
        anchor: dict[str, Any],
    ) -> CompiledQuery:
        """A time series that includes the periods with no rows.

        The window average used to run over whichever rows existed. For a
        facility that bought in June and September only, the September
        "3-month average" averaged June with September -- a window that does
        not contain June -- and July and August did not appear at all. And
        the first points of every series averaged fewer periods than asked,
        even when the earlier months were in the data but outside the window.

        Built on app_ref.calendar, the dataset's own reporting calendar:

        1. every period of the plan's grain, numbered oldest first;
        2. the periods the window asks for -- selected by applying the SAME
           resolved window predicate to the calendar as to the facts, so
           there is one definition of "last quarter", not two;
        3. for a rolling average, the N-1 periods before those as well, so
           the first requested point averages a full window;
        4. the metric, computed over exactly those periods;
        5. every period crossed with every group, with the facts left-joined.

        An absent period is ZERO for an additive metric when every source the
        metric reads had rows in it -- nothing was bought. It is UNKNOWN
        where a source did not cover it, and always unknown for a ratio, whose
        missing row means an undefined denominator. A rolling average is
        given only when all N periods are known: a shorter average presented
        as an N-period one is the defect this replaces.
        """
        kind = spec.get("kind")
        period_index = next(i for i, d in enumerate(plan.dimensions)
                            if d in PERIOD_COLUMN)
        column = PERIOD_COLUMN[plan.dimensions[period_index]]
        partition = [i for i in range(len(plan.dimensions)) if i != period_index]
        # A rolling average reaches N-1 periods back; a period-over-period
        # change reaches one, so the window's first period has its prior.
        span = (plan.rolling.periods if plan.rolling is not None
                else 2 if plan.period_over_period else 1)
        params: list[Any] = []

        # 1. every period of this grain, with whether its sources covered it
        sources = self._sources_of(spec)
        coverage = " AND ".join(["%s = ANY(c.sources)"] * len(sources)) or "TRUE"
        params += sources
        ctes = [
            "pac_periods AS (\n"
            f"  SELECT c.{column} AS label, min(c.wk_offset) AS newest, count(*) AS weeks,\n"
            # min(...) = 1 rather than bool_and, which sqlglot renames to
            # logical_and -- an allowlist entry that depends on parser naming.
            f"         min(CASE WHEN {coverage} THEN 1 ELSE 0 END) = 1 AS covered\n"
            "  FROM app_ref.calendar c\n"
            f"  GROUP BY c.{column})",
            "pac_numbered AS (\n"
            "  SELECT label, covered, weeks,\n"
            "         row_number() OVER (ORDER BY newest DESC) AS pos\n"
            "  FROM pac_periods)",
        ]

        # 2. the periods the window asks for -- the facts' own predicate
        on_calendar = resolve(plan.time, anchor, alias="c")
        ctes.append(
            "pac_requested AS (\n"
            f"  SELECT DISTINCT c.{column} AS label FROM app_ref.calendar c\n"
            f"  WHERE {on_calendar.sql})")
        params += on_calendar.params

        # 3. extended backwards by span - 1 for a rolling average
        ctes.append(
            "pac_span AS (\n"
            "  SELECT min(n.pos) - %s AS lo, max(n.pos) AS hi\n"
            "  FROM pac_numbered n JOIN pac_requested r ON r.label = n.label)")
        params.append(span - 1)
        ctes.append(
            "pac_spine AS (\n"
            "  SELECT n.label, n.covered, n.weeks, n.pos,\n"
            "         n.label IN (SELECT label FROM pac_requested) AS requested\n"
            "  FROM pac_numbered n CROSS JOIN pac_span sp\n"
            "  WHERE n.pos BETWEEN sp.lo AND sp.hi)")

        # 4. the metric over exactly the spine's periods
        over_spine = ResolvedWindow(
            sql=f"s.{column} IN (SELECT label FROM pac_spine)",
            params=[], label=window.label)
        extra_cols: list[str] = []
        if kind == "ratio":
            series = self._compile_ratio(plan, spec, over_spine, anchor, apply_limit=False)
            series_sql, series_params = series.sql, series.params
            extra_cols = ["numerator", "denominator"]
        else:
            series_sql, series_params, _ = self._leaf_select(
                plan.metric.value, plan.filters, over_spine, dims=list(plan.dimensions))
        ctes.append(f"pac_series AS (\n{series_sql})")
        params += series_params

        # 5. every period for every group
        dim_cols = lambda alias, i: f"{alias}.dim{i}_id, {alias}.dim{i}_label"  # noqa: E731
        if partition:
            ctes.append(
                "pac_parts AS (\n  SELECT DISTINCT "
                + ", ".join(dim_cols("se", i) for i in partition)
                + "\n  FROM pac_series se)")
        additive = kind in (None, "count_distinct")
        fill = "COALESCE(se.value, 0)" if additive else "se.value"
        select_dims = []
        for i in range(len(plan.dimensions)):
            if i == period_index:
                select_dims.append(f"sp.label AS dim{i}_id, sp.label AS dim{i}_label")
            else:
                select_dims.append(f"pt.dim{i}_id, pt.dim{i}_label")
        join = [f"se.dim{period_index}_id = sp.label"]
        for i in partition:
            join.append(f"{_join_key('se', f'dim{i}_id')} = {_join_key('pt', f'dim{i}_id')}")
            params += [NULL_KEY_SENTINEL, NULL_KEY_SENTINEL]
        extras = "".join(f",\n         se.{c}" for c in extra_cols)
        ctes.append(
            "pac_dense AS (\n"
            f"  SELECT {', '.join(select_dims)}, sp.pos, sp.requested, sp.weeks,\n"
            f"         CASE WHEN sp.covered THEN {fill} END AS value{extras}\n"
            "  FROM pac_spine sp\n"
            + ("  CROSS JOIN pac_parts pt\n" if partition else "")
            + f"  LEFT JOIN pac_series se ON {' AND '.join(join)})")

        carried = ", ".join(f"dim{i}_id, dim{i}_label" for i in range(len(plan.dimensions)))
        carried_extras = "".join(f", {c}" for c in extra_cols)
        if plan.rolling is not None:
            over = (
                (f"PARTITION BY {', '.join(f'dim{i}_id' for i in partition)} "
                 if partition else "")
                + f"ORDER BY pos ROWS BETWEEN {span - 1} PRECEDING AND CURRENT ROW")
            ctes.append(
                "pac_rolled AS (\n"
                f"  SELECT {carried}{carried_extras}, requested, value AS point_value,\n"
                f"         CASE WHEN count(value) OVER ({over}) = %s\n"
                f"              THEN avg(value) OVER ({over}) END AS value\n"
                "  FROM pac_dense)")
            params.append(span)
            body = (f"WITH {', '.join(ctes)}\n"
                    f"SELECT {carried}, point_value{carried_extras}, value\n"
                    "FROM pac_rolled WHERE requested\n")
        elif plan.period_over_period:
            # The prior is the same group's previous period on the spine --
            # NULL for the first period of the data, and NULL where that
            # period is unknown. A percentage needs a positive prior: from zero
            # it is undefined, and from a negative total its sign would invert
            # the direction. The absolute change is given whenever both
            # periods are known.
            over = ((f"PARTITION BY {', '.join(f'dim{i}_id' for i in partition)} "
                     if partition else "") + "ORDER BY pos")
            ctes.append(
                "pac_lagged AS (\n"
                f"  SELECT {carried}{carried_extras}, requested, value, weeks,\n"
                f"         lag(value) OVER ({over}) AS prior_value,\n"
                f"         lag(weeks) OVER ({over}) AS prior_weeks\n"
                "  FROM pac_dense)")
            body = (f"WITH {', '.join(ctes)}\n"
                    f"SELECT {carried}, prior_value,\n"
                    "       value - prior_value AS change,\n"
                    "       CASE WHEN prior_value > 0\n"
                    "            THEN (value - prior_value)::numeric / prior_value END AS change_pct,\n"
                    f"       weeks, prior_weeks{carried_extras}, value\n"
                    "FROM pac_lagged WHERE requested\n")
        else:
            body = (f"WITH {', '.join(ctes)}\n"
                    f"SELECT {carried}{carried_extras}, value\n"
                    "FROM pac_dense WHERE requested\n")

        sql, params = self._finish(body, plan, params, rolled=True)

        notes: list[str] = []
        if additive:
            notes.append(
                "A period with no purchases is shown as zero. A period a data "
                "source did not cover is left blank, not zero.")
        else:
            notes.append(
                "A period with no data is left blank: a share with nothing to "
                "divide by is undefined, not zero.")
        if plan.period_over_period:
            grain = GRAIN_WORD[plan.dimensions[period_index]]
            notes.append(
                f"Each {grain} is compared with the {grain} before it, including the "
                f"{grain} just before the window. The change is blank for the first "
                f"{grain} of the data, and wherever either {grain} is unknown. The "
                f"percentage is blank where the earlier {grain} was zero or negative; "
                f"the change in {spec['unit']} is still shown.")
            if plan.dimensions[period_index] is not Dimension.period_wk:
                notes.append(
                    f"A reporting {grain} holds the weeks that end in it, so {grain}s "
                    f"differ in length (4 or 5 weeks for a month); a total moves with its "
                    f"number of weeks even when the weekly rate does not. Each row shows "
                    f"both {grain}s' week counts.")
        if plan.rolling is not None:
            notes.append(
                f"Each figure averages that period and the {span - 1} before it, "
                f"including periods before the window starts. It is left blank "
                f"where any of those {span} periods is outside the data or "
                f"unknown, rather than averaging fewer.")
        columns = (self._columns(plan, extra=extra_cols + ["value"])
                   if extra_cols else self._columns(plan))
        current = (anchor.get(f"max_{column}")
                   if plan.period_over_period and window.incomplete_period else None)
        return CompiledQuery(
            sql=sql, params=params, columns=columns,
            unit=spec["unit"], metric_label=spec["label"], window_label=window.label,
            current_period=current,
            notes=notes,
            quality_checks=list(spec.get("quality_checks", [])) if kind == "ratio" else [],
        )

    def _compile_simple(
        self, plan: AnalyticalPlan, spec: dict[str, Any], window: ResolvedWindow
    ) -> CompiledQuery:
        body, params, needs = self._leaf_select(
            plan.metric.value, plan.filters, window, dims=list(plan.dimensions)
        )
        sql, params = self._finish(body, plan, params)
        return CompiledQuery(
            sql=sql, params=params, columns=self._columns(plan),
            unit=spec["unit"], metric_label=spec["label"], window_label=window.label,
        )

    def _split_grains(
        self, dims: list[Dimension]
    ) -> tuple[list[Dimension], bool]:
        """Return the grain the DENOMINATOR can be grouped by, and whether a
        product-level bridge was needed.

        A product grain (drug name, NDC, strength) has no meaning in market data
        at the company-product level, so it is replaced by market_subcategory.
        Territory, account, period and the like are shared grains and pass
        through unchanged.
        """
        bridged = False
        out: list[Dimension] = []
        for dim in dims:
            if dim in PRODUCT_GRAINS:
                bridged = True
                if BRIDGE_DIMENSION not in out:
                    out.append(BRIDGE_DIMENSION)
            elif dim not in out:
                out.append(dim)
        return out, bridged

    def _compile_ratio(
        self,
        plan: AnalyticalPlan,
        spec: dict[str, Any],
        window: ResolvedWindow,
        anchor: dict[str, Any],
        *,
        apply_limit: bool = True,
    ) -> CompiledQuery:
        """apply_limit=False when this ratio is one SIDE of a comparison.

        A display cap is about how much to show, so it belongs to the final
        answer. Applied to a comparison's input it silently changes what the
        answer means: each period is cut to its top rows BY SHARE, and the
        change is then computed from two different truncated populations. A
        product ranked outside the cap this period but with the largest share
        movement disappears from the result entirely -- which is precisely the
        row the question was asking for.
        """
        num_key, den_key = spec["numerator"], spec["denominator"]
        subcategory_scoped = spec.get("denominator_scope") == "market_subcategory"

        requested = list(plan.dimensions)
        if subcategory_scoped:
            den_dims, bridged = self._split_grains(requested)
        else:
            den_dims, bridged = list(requested), False

        # The numerator reports the requested grains and additionally projects
        # the denominator's grain as join keys, so the two meet correctly.
        num_join_dims = [d for d in den_dims if d not in requested]

        den_extra_clauses: list[str] = []
        den_extra_params: list[Any] = []
        if subcategory_scoped:
            # A3: the denominator covers EVERY market_data row in the same market
            # subcategory as the numerator's products -- never narrowed to the
            # company drug name, and never restricted to brand_flag = 1.
            f = plan.filters
            if f.market_subcategories:
                # The user named the market, so use it directly. Deriving it
                # from our own brands instead would return nothing for a market
                # we do not compete in, turning "what is the Carboplatin
                # market" into an empty answer rather than a real one.
                den_extra_clauses.append("p.market_subcategory = ANY(%s)")
                den_extra_params.append(list(f.market_subcategories))
            elif f.market_categories:
                den_extra_clauses.append("p.market_category = ANY(%s)")
                den_extra_params.append(list(f.market_categories))
            else:
                # No market named: infer it from the company products the
                # numerator covers, so ZENOVAX is measured against Docetaxel.
                sub_clauses = ["brand_flag = 1"]
                sub_params: list[Any] = []
                if f.product_names:
                    sub_clauses.append("upper(drug_name) = ANY(%s)")
                    sub_params.append([v.upper() for v in f.product_names])
                if f.ndcs:
                    sub_clauses.append("ndc = ANY(%s)")
                    sub_params.append(list(f.ndcs))
                den_extra_clauses.append(
                    "p.market_subcategory IN (SELECT DISTINCT market_subcategory FROM products"
                    f" WHERE {' AND '.join(sub_clauses)})"
                )
                den_extra_params += sub_params

        # What the denominator is a proportion OF is declared by the metric,
        # not decided here.
        #
        # For market share the denominator must NOT inherit product identity:
        # keeping the drug-name filter would make "the market" equal our own
        # sales, and share would always be 100%.
        #
        # For PAP share both sides describe the same products, and widening
        # the denominator turns "what proportion of Zenovax volume was free"
        # into "Zenovax free volume as a share of everything we sold" --
        # 20/175 rather than 20/130. Same units, same shape, quietly wrong.
        population = spec["denominator_population"]
        num_filters = plan.filters
        if population == "ignores_340b":
            # The metric defines BOTH sides. The numerator is the 340B subset
            # and the denominator is the same population with the condition
            # lifted -- neither is left to whatever the planner happened to
            # set. Relying on the planner for the numerator meant a model that
            # chose share_340b with is_340b="include" produced a numerator
            # identical to its denominator and reported 100%: a confident,
            # meaningless answer. The offline planner set it correctly, so
            # only the live model exposed this.
            num_filters = plan.filters.model_copy(
                update={"is_340b": TriState.only})
            den_filters = plan.filters.model_copy(
                update={"is_340b": TriState.include})
        elif population == "surrounding_market":
            den_filters = plan.filters.model_copy(
                update={"product_names": [], "ndcs": [], "strengths": [],
                        "classifications": []}
            )
        else:
            den_filters = plan.filters

        num_sql, num_params, num_needs = self._leaf_select(
            num_key, num_filters, window, dims=requested, join_dims=num_join_dims
        )
        den_sql, den_params, den_needs = self._leaf_select(
            den_key, den_filters, window,
            dims=[], join_dims=den_dims,
            extra_clauses=den_extra_clauses, extra_params=den_extra_params,
        )

        join_params: list[Any] = []
        if den_dims:
            # Numerator side: a den_dim is either one of the requested dims
            # (dim{i}_id) or one of the projected join keys (jk{j}).
            conds = []
            for j, dim in enumerate(den_dims):
                if dim in requested:
                    left = f"dim{requested.index(dim)}_id"
                else:
                    left = f"jk{num_join_dims.index(dim)}"
                conds.append(f"{_join_key('n', left)} = {_join_key('d', f'jk{j}')}")
                join_params += [NULL_KEY_SENTINEL, NULL_KEY_SENTINEL]
            join_cond = " AND ".join(conds)
            # When a product grain was bridged, only numerator rows are
            # meaningful (a competitor product is never in our numerator), so
            # the numerator drives. Otherwise keep both sides so a market with
            # no company volume is still visible.
            join_type = "LEFT JOIN" if bridged else "FULL OUTER JOIN"
            if requested:
                dims_sql = ", ".join(
                    f"n.dim{i}_id AS dim{i}_id, n.dim{i}_label AS dim{i}_label"
                    if bridged else
                    f"COALESCE(n.dim{i}_id, d.jk{den_dims.index(requested[i])}) AS dim{i}_id, "
                    f"n.dim{i}_label AS dim{i}_label"
                    for i in range(len(requested))
                )
            else:
                dims_sql = ""
            select_head = f"{dims_sql}," if dims_sql else ""
            sql = (
                f"WITH num AS (\n{num_sql}), den AS (\n{den_sql})\n"
                f"SELECT {select_head}\n"
                "       n.value AS numerator, d.value AS denominator,\n"
                # A3: zero or missing denominator -> NULL, never 0, never a
                # division-by-zero error.
                "       n.value / NULLIF(d.value, 0) AS value\n"
                f"FROM num n {join_type} den d ON {join_cond}\n"
            )
        else:
            sql = (
                f"WITH num AS (\n{num_sql}), den AS (\n{den_sql})\n"
                "SELECT n.value AS numerator, d.value AS denominator,\n"
                "       n.value / NULLIF(d.value, 0) AS value\n"
                "FROM num n CROSS JOIN den d\n"
            )
        ratio_params = num_params + den_params + join_params
        if apply_limit:
            sql, ratio_params = self._finish(sql, plan, ratio_params)

        notes: list[str] = []
        if bridged:
            notes.append(
                "Share is measured against the whole market subcategory each product "
                "competes in, not against that product alone."
            )

        return CompiledQuery(
            sql=sql,
            params=ratio_params,
            columns=self._columns(plan, extra=["numerator", "denominator", "value"]),
            unit=spec["unit"], metric_label=spec["label"], window_label=window.label,
            notes=notes,
            quality_checks=list(spec.get("quality_checks", [])),
        )

    def _compile_change(
        self,
        plan: AnalyticalPlan,
        spec: dict[str, Any],
        window: ResolvedWindow,
        anchor: dict[str, Any],
    ) -> CompiledQuery:
        if plan.comparison is None:
            raise CompileError(f"{plan.metric} requires a comparison window")

        # Period grains are refused in check_compatibility, before this runs.

        prior = resolve(plan.comparison, anchor)
        base_key = spec["base_metric"]
        base_spec = self.registry.get(base_key)

        def side(w: ResolvedWindow) -> tuple[str, list[Any]]:
            sub = plan.model_copy(update={"metric": MetricKey(base_key), "ranking": None})
            if base_spec.get("kind") == "ratio":
                # No display cap on an input: see _compile_ratio's docstring.
                q = self._compile_ratio(sub, base_spec, w, anchor, apply_limit=False)
                return q.sql, q.params
            body, params, needs = self._leaf_select(
                base_key, plan.filters, w, dims=list(plan.dimensions)
            )
            return body, params

        cur_sql, cur_params = side(window)
        pri_sql, pri_params = side(prior)

        change = spec["change"]
        if change == "relative":
            # A11: a zero prior period is new activity, not infinite growth.
            value = "(c.value - p.value) / NULLIF(p.value, 0)"
        elif change == "absolute":
            # A11: a share delta is percentage POINTS, not percent growth.
            value = "(c.value - p.value) * 100.0"
        else:
            weight = "(COALESCE(c.denominator, 0) + COALESCE(p.denominator, 0))"
            value = f"(c.value - p.value) * {weight}"

        join_params: list[Any] = []
        if plan.dimensions:
            keys = [f"dim{i}_id" for i in range(len(plan.dimensions))]
            cond = " AND ".join(
                f"{_join_key('c', k)} = {_join_key('p', k)}" for k in keys
            )
            join_params = [NULL_KEY_SENTINEL] * (2 * len(keys))
            dims = ", ".join(
                f"COALESCE(c.dim{i}_id, p.dim{i}_id) AS dim{i}_id, "
                f"COALESCE(c.dim{i}_label, p.dim{i}_label) AS dim{i}_label"
                for i in range(len(plan.dimensions))
            )
            sql = (
                f"WITH cur AS (\n{cur_sql}), pri AS (\n{pri_sql})\n"
                f"SELECT {dims}, c.value AS current_value, p.value AS prior_value,\n"
                f"       {value} AS value\n"
                f"FROM cur c FULL OUTER JOIN pri p ON {cond}\n"
            )
        else:
            sql = (
                f"WITH cur AS (\n{cur_sql}), pri AS (\n{pri_sql})\n"
                f"SELECT c.value AS current_value, p.value AS prior_value, {value} AS value\n"
                "FROM cur c CROSS JOIN pri p\n"
            )
        sql, change_params = self._finish(
            sql, plan, cur_params + pri_params + join_params)

        notes = []
        if change == "relative":
            notes.append(
                "Accounts with no prior-period volume are reported as new activity "
                "rather than as a growth percentage."
            )
        if change == "absolute":
            notes.append("Reported in percentage points, not percent growth.")

        return CompiledQuery(
            sql=sql,
            params=change_params,
            columns=self._columns(plan, extra=["current_value", "prior_value", "value"]),
            unit=spec["unit"], metric_label=spec["label"], window_label=window.label,
            comparison_label=prior.label, notes=notes,
        )
