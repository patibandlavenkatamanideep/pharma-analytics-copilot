"""The schema this system is able to answer questions about.

The generalisation promise is narrow and worth stating precisely: **new rows,
new names, new values, new periods and new combinations within the supplied
schema**. A different schema is not supported.

That distinction has to be enforced, not just documented. Given a table whose
columns have moved, a compiler built on fixed identifiers will either fail with
an obscure SQL error deep inside a query, or -- worse -- succeed against
columns that happen to still exist and answer with the wrong numbers.

So the contract is checked before anything is loaded, and a mismatch fails the
load with a message naming exactly what differs. The fingerprint is recorded in
the manifest, so any stored result can be traced to the schema that produced it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "1.0.0"

# The five supplied tables and every column the supplied DDL defines, with the
# PostgreSQL type each must have.
#
# The full supplied set rather than only the columns we read: a database missing
# `city` is not the schema this was built against, even though no query touches
# it, and saying so early is more useful than discovering it later. Columns
# BEYOND this set are compatible and ignored -- an additive migration must not
# break a working deployment.
REQUIRED: dict[str, dict[str, str]] = {
    "organizations": {
        "org_id": "text", "org_name": "text", "org_type": "text",
        "org_status": "text", "org_archetype": "text", "specialty": "text",
        "address_line1": "text", "city": "text", "state": "text", "zip": "text",
        "parent_org_id": "text", "parent_org_name": "text",
        "grandparent_org_id": "text", "grandparent_org_name": "text",
        "gpo_name": "text", "is_340b": "integer",
    },
    "products": {
        "ndc": "text", "drug_name": "text", "generic_name": "text",
        "strength": "text", "form": "text", "brand_flag": "integer",
        "specialty": "text", "market_category": "text",
        "market_subcategory": "text",
        "unit_conversion_factor": "double precision",
        "mg_equivalent": "double precision",
    },
    "sales": {
        "org_id": "text", "ndc": "text", "drug_name": "text",
        "data_source": "text", "brand_flag": "integer",
        "pack_units": "double precision", "total_mg": "double precision",
        "wac": "double precision",
        "transaction_date": "text", "week_ending_date": "text",
        "state": "text", "specialty": "text",
        "period_wk": "text", "period_mo": "text", "period_qtr": "text",
        "wk_offset": "integer", "mo_offset": "integer",
    },
    "zip_territory": {
        "zip": "text", "state": "text",
        "territory_number": "text", "territory_name": "text",
        "region_number": "text", "region_name": "text",
    },
    "users": {
        "user_id": "text", "email": "text", "full_name": "text", "role": "text",
        "territory_name": "text", "region_name": "text", "can_view_wac": "integer",
    },
}


class SchemaIncompatible(RuntimeError):
    """The database does not match the contract this system can answer about."""


@dataclass
class SchemaCheck:
    compatible: bool
    fingerprint: str
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    wrong_types: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)

    def report(self) -> str:
        if self.compatible:
            extra = (
                f" ({len(self.extra_columns)} additional column(s) present and ignored)"
                if self.extra_columns else ""
            )
            return f"schema matches contract v{CONTRACT_VERSION}{extra}"

        lines = [
            "This database does not match the schema this assistant can answer "
            f"questions about (contract v{CONTRACT_VERSION}).",
            "",
        ]
        if self.missing_tables:
            lines.append(f"  missing tables:  {', '.join(sorted(self.missing_tables))}")
        if self.missing_columns:
            lines.append(f"  missing columns: {', '.join(sorted(self.missing_columns))}")
        if self.wrong_types:
            lines.append("  wrong types:")
            lines += [f"    {w}" for w in sorted(self.wrong_types)]
        lines += [
            "",
            "Supported: new rows, names, values, periods and combinations within "
            "the supplied schema.",
            "Not supported: a different schema. Mapping one would need validated "
            "join definitions, which this system deliberately does not infer.",
        ]
        return "\n".join(lines)


def observed_columns(cur: Any) -> dict[str, dict[str, str]]:
    cur.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        ORDER BY table_name, column_name
        """,
        (list(REQUIRED),),
    )
    out: dict[str, dict[str, str]] = {}
    for row in cur.fetchall():
        out.setdefault(row["table_name"], {})[row["column_name"]] = row["data_type"]
    return out


def fingerprint(observed: dict[str, dict[str, str]]) -> str:
    """A stable hash of the structure the application depends on.

    Only contract columns contribute, so adding an unrelated column does not
    change the fingerprint and does not invalidate stored results.
    """
    parts = []
    for table in sorted(REQUIRED):
        columns = observed.get(table, {})
        for column in sorted(REQUIRED[table]):
            parts.append(f"{table}.{column}:{columns.get(column, '<missing>')}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def check(cur: Any) -> SchemaCheck:
    observed = observed_columns(cur)
    missing_tables, missing_columns, wrong_types, extra_columns = [], [], [], []

    for table, columns in REQUIRED.items():
        found = observed.get(table)
        if found is None:
            missing_tables.append(table)
            continue
        for column, expected_type in columns.items():
            actual = found.get(column)
            if actual is None:
                missing_columns.append(f"{table}.{column}")
            elif actual != expected_type:
                wrong_types.append(
                    f"{table}.{column}: expected {expected_type}, found {actual}"
                )
        for column in found:
            if column not in columns and column != "sale_id":
                extra_columns.append(f"{table}.{column}")

    return SchemaCheck(
        compatible=not (missing_tables or missing_columns or wrong_types),
        fingerprint=fingerprint(observed),
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        wrong_types=wrong_types,
        extra_columns=extra_columns,
    )


def require_compatible(cur: Any) -> SchemaCheck:
    result = check(cur)
    if not result.compatible:
        raise SchemaIncompatible(result.report())
    return result
