"""Final SQL validation.

This is a defence-in-depth layer, not the security boundary. The boundary is the
database: RLS for rows, per-column grants for pricing. This module exists so a
compiler bug is caught before it reaches PostgreSQL, and so the shape of what we
execute is provably narrow.

It parses the COMPLETE statement into an AST with sqlglot and inspects every
node. Regex matching is deliberately not used -- a comment, a string literal or
an unusual whitespace run defeats it. Validation runs on the FINAL SQL text,
after every rewrite, and the same text is what gets executed.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

DIALECT = "postgres"

# Only these relations may appear. The supplied users table and every app_*
# schema are absent by construction.
ALLOWED_TABLES = {
    "sales",
    "organizations",
    "products",
    "zip_territory",
    "product_classification",       # app_ref, reached via search_path
    "app_ref.product_classification",
}

ALLOWED_FUNCTIONS = {
    "sum", "count", "avg", "min", "max", "coalesce", "nullif", "round",
    "upper", "lower", "abs", "any", "cast", "to_char", "greatest", "least",
    "distinct",
}

# Anything that changes session state, reaches the filesystem or network, or
# reads catalog/identity data.
FORBIDDEN_FUNCTIONS = {
    "set_config", "current_setting", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "lo_import", "lo_export", "dblink", "dblink_connect",
    "pg_sleep", "pg_terminate_backend", "pg_cancel_backend", "query_to_xml",
    "pg_stat_file", "copy", "current_user", "session_user", "current_database",
    "has_table_privilege", "has_column_privilege", "generate_series",
    "pg_notify", "set_role",
}

FORBIDDEN_SCHEMAS = {"pg_catalog", "information_schema", "app_auth", "app_conv", "app_meta"}

# Statement types that must never appear anywhere in the tree.
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Grant, exp.Merge, exp.TruncateTable, exp.Command, exp.Set,
)


class SqlValidationError(ValueError):
    pass


@dataclass
class ValidationResult:
    ok: bool
    tables: set[str]
    functions: set[str]


def _to_parseable(sql: str) -> str:
    """Normalize psycopg parameter markers so the statement can be parsed.

    sqlglot reads `%s` as a modulo operator, so each marker is swapped for a `?`
    placeholder before parsing. This is a marker-for-marker substitution: it
    changes no identifier, no function, no clause and no literal, so the tree
    that gets inspected is structurally the statement that gets executed. The
    text sent to PostgreSQL is always the ORIGINAL sql -- this rewrite exists
    only inside the parser.

    `%%` is psycopg's escape for a literal percent sign, so it is protected
    first; otherwise `%%s` inside a string literal would be misread as a marker.
    """
    guard = "\x00PCT\x00"
    return sql.replace("%%", guard).replace("%s", "?").replace(guard, "%%")


def validate(sql: str, *, wac_authorized: bool) -> ValidationResult:
    """Validate the final SQL. Raises SqlValidationError on anything unexpected."""
    try:
        statements = sqlglot.parse(_to_parseable(sql), dialect=DIALECT)
    except Exception as exc:
        raise SqlValidationError(f"could not parse SQL: {exc}") from None

    # Multiple statements would let a second one run outside every check above.
    real = [s for s in statements if s is not None]
    if len(real) != 1:
        raise SqlValidationError(f"expected exactly one statement, found {len(real)}")

    tree = real[0]
    if not isinstance(tree, exp.Select):
        raise SqlValidationError(f"only SELECT is permitted, found {type(tree).__name__}")

    for node_type in FORBIDDEN_NODES:
        if list(tree.find_all(node_type)):
            raise SqlValidationError(f"forbidden statement node: {node_type.__name__}")

    # A data-modifying CTE hides a write inside a SELECT.
    for cte in tree.find_all(exp.CTE):
        inner = cte.this
        if not isinstance(inner, (exp.Select, exp.Union, exp.Subquery)):
            raise SqlValidationError(f"CTE must be a SELECT, found {type(inner).__name__}")

    # Recursion is an unbounded-work primitive.
    for with_node in tree.find_all(exp.With):
        if with_node.args.get("recursive"):
            raise SqlValidationError("recursive CTEs are not permitted")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}

    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        schema = (table.db or "").lower()
        if name in cte_names and not schema:
            continue                      # a reference to our own CTE
        if schema in FORBIDDEN_SCHEMAS:
            raise SqlValidationError(f"forbidden schema: {schema}")
        qualified = f"{schema}.{name}" if schema else name
        if qualified not in ALLOWED_TABLES and name not in ALLOWED_TABLES:
            raise SqlValidationError(f"relation not on the allowlist: {qualified}")
        tables.add(qualified)

    functions: set[str] = set()
    for func in tree.find_all(exp.Func):
        fname = (func.sql_name() or type(func).__name__).lower()
        functions.add(fname)
        if fname in FORBIDDEN_FUNCTIONS:
            raise SqlValidationError(f"forbidden function: {fname}")
    for anon in tree.find_all(exp.Anonymous):
        fname = (anon.name or "").lower()
        functions.add(fname)
        if fname in FORBIDDEN_FUNCTIONS or fname not in ALLOWED_FUNCTIONS:
            raise SqlValidationError(f"function not on the allowlist: {fname}")

    # Pricing. This repeats a check the database already enforces through column
    # grants; if these ever disagree, the database wins and this raises first.
    if not wac_authorized:
        for column in tree.find_all(exp.Column):
            if column.name.lower() == "wac":
                raise SqlValidationError(
                    "wac referenced by a principal without pricing authorization"
                )
        # A star is only dangerous where it can resolve against sales -- that
        # serializes wac without naming it. A star over a CTE is fine, because
        # the CTE's own projection was already checked by this same pass.
        for select in tree.find_all(exp.Select):
            if not any(isinstance(e, exp.Star) for e in select.expressions):
                continue
            scope_tables = {
                t.name.lower()
                for t in select.find_all(exp.Table)
                if t.parent_select is select
            }
            if "sales" in scope_tables:
                raise SqlValidationError(
                    "wildcard selection over sales is not permitted without "
                    "pricing authorization"
                )
        for alias in tree.find_all(exp.Alias):
            if alias.alias and alias.alias.lower() in {"wac", "revenue", "gross_revenue"}:
                raise SqlValidationError(f"alias {alias.alias!r} implies pricing data")

    return ValidationResult(ok=True, tables=tables, functions=functions)
