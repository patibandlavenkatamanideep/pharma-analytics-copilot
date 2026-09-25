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


class CompileError(ValueError):
    pass


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


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]
    columns: list[str]
    unit: str
    metric_label: str
    window_label: str
    comparison_label: str | None = None
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

        if extra_clauses:
            where += extra_clauses
            params += extra_params or []

        # A structural count is about the hierarchy, not about transactions,
        # so it reads organizations directly. Row-level security still applies
        # -- organizations carries the same policy as sales -- so scope is
        # enforced exactly as it is everywhere else.
        if kind == "count_structural":
            # organizations is the FROM table here, so it must not also be
            # joined in.
            needs.discard("o")
            sql = (
                f"SELECT {', '.join(select_parts)}\n"
                f"FROM organizations o\n{{joins}}\n"
                f"WHERE {' AND '.join(where) if where else 'TRUE'}\n"
            )
        else:
            sql = (
                f"SELECT {', '.join(select_parts)}\n"
                f"FROM sales s\n{{joins}}\n"
                f"WHERE {' AND '.join(where) if where else 'TRUE'}\n"
            )
        if group_parts:
            sql += f"GROUP BY {', '.join(group_parts)}\n"
        return sql, params, needs

    def _render_joins(self, needs: set[str]) -> str:
        # z depends on o; c depends on p. Order is fixed, not data-dependent.
        if "z" in needs:
            needs.add("o")
        if "c" in needs:
            needs.add("p")
        return "\n".join(JOINS[k] for k in ("o", "p", "z", "c") if k in needs)

    # -- public entry point --------------------------------------------------

    def compile(
        self,
        plan: AnalyticalPlan,
        *,
        anchor: dict[str, Any],
    ) -> CompiledQuery:
        spec = self.registry.get(plan.metric.value)
        kind = spec.get("kind")
        window = resolve(plan.time, anchor)
        notes: list[str] = list(window.caveats)
        if window.incomplete_period:
            notes.append(
                "This window includes the current reporting period, which is still "
                "accumulating."
            )

        if kind == "ratio":
            query = self._compile_ratio(plan, spec, window, anchor)
        elif kind == "period_change":
            query = self._compile_change(plan, spec, window, anchor)
        else:
            query = self._compile_simple(plan, spec, window)

        query.notes = notes + query.notes
        query.window_label = window.label
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

    def _order_and_limit(self, plan: AnalyticalPlan, value_col: str = "value") -> str:
        out = ""
        if plan.ranking:
            direction = "DESC" if plan.ranking.direction == "top" else "ASC"
            out += f"ORDER BY {value_col} {direction} NULLS LAST\n"
            out += f"LIMIT {int(plan.ranking.limit)}\n"
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
        cols += extra or ["value"]
        return cols

    def _compile_simple(
        self, plan: AnalyticalPlan, spec: dict[str, Any], window: ResolvedWindow
    ) -> CompiledQuery:
        body, params, needs = self._leaf_select(
            plan.metric.value, plan.filters, window, dims=list(plan.dimensions)
        )
        sql = body.format(joins=self._render_joins(needs)) + self._order_and_limit(plan)
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
        num_sql = num_sql.format(joins=self._render_joins(num_needs))
        den_sql = den_sql.format(joins=self._render_joins(den_needs))

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
        if apply_limit:
            sql += self._order_and_limit(plan)

        notes: list[str] = []
        if bridged:
            notes.append(
                "Share is measured against the whole market subcategory each product "
                "competes in, not against that product alone."
            )

        return CompiledQuery(
            sql=sql,
            params=num_params + den_params + join_params,
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

        # A period grain inside a two-window comparison cannot be joined.
        # The current side is labelled 2026-Q3 and the prior side 2026-Q2, so
        # the FULL JOIN matches nothing: every row comes back with one side
        # null and a null growth figure. It looks like "no growth data" rather
        # than like a question the system cannot express.
        #
        # Aligning by position instead would be a guess about what the user
        # meant. What they almost certainly want -- a growth figure for each
        # period against its own prior -- is a different query shape, not this
        # one. So it is refused by name, with the two things that do work.
        period_dims = [d for d in plan.dimensions
                       if d in (Dimension.period_mo, Dimension.period_qtr,
                                Dimension.period_wk)]
        if period_dims:
            grain = period_dims[0].value.replace("period_", "")
            raise CompileError(
                f"A {grain}-by-{grain} breakdown cannot also be a two-window "
                f"comparison: each side would be labelled with a different "
                f"{grain}, so nothing lines up. Ask for the trend "
                f"(\"volume by {grain}\") to see the series, or drop the "
                f"{grain} breakdown to see one growth figure for the window."
            )

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
            return body.format(joins=self._render_joins(needs)), params

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
        sql += self._order_and_limit(plan)

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
            params=cur_params + pri_params + join_params,
            columns=self._columns(plan, extra=["current_value", "prior_value", "value"]),
            unit=spec["unit"], metric_label=spec["label"], window_label=window.label,
            comparison_label=prior.label, notes=notes,
        )
