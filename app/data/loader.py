"""Ingestion.

seed and full are mutually exclusive business-data modes: loading one truncates
the other, because the seed fixture and the generated CSVs reuse the same
reference keys and mixing them would silently contaminate every aggregate.

Users are bootstrapped separately in both modes, since the generator emits no
users CSV and the supplied users table is the authority for role and scope.

Validation distinguishes two outcomes. A documented data anomaly (impossible
market share, unmapped ZIP, period/transaction month mismatch) is recorded as a
manifest WARNING and the load proceeds -- refusing would make the supplied
dataset unusable. A condition that makes safe execution or the requested metric
impossible (missing table, broken foreign key, unknown data_source) FAILS the
load and the snapshot is never published.
"""

from __future__ import annotations

import csv
import io
import pathlib
import uuid
from typing import Any

from psycopg import sql

from app.data.classification import RULE_VERSION, classify
from app.data.manifest import MAPPING_VERSION, LoadReport, file_sha256
from app.data.schema_contract import CONTRACT_VERSION, require_compatible
from app.db import owner_transaction

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
GENERATED = ROOT / "schema" / "generated"
SEED_SQL = ROOT / "schema" / "seed_data.sql"

BUSINESS_TABLES = ("sales", "organizations", "products", "zip_territory")

VALID_SOURCES = {"distributor", "hub_dispense", "market_data"}

# Column lists must match the generator's CSV headers exactly.
ORG_COLS = [
    "org_id", "org_name", "org_type", "org_status", "org_archetype",
    "specialty", "address_line1", "city", "state", "zip",
    "parent_org_id", "parent_org_name",
    "grandparent_org_id", "grandparent_org_name", "gpo_name", "is_340b",
]
PRODUCT_COLS = [
    "ndc", "drug_name", "generic_name", "strength", "form",
    "brand_flag", "specialty", "market_category", "market_subcategory",
    "unit_conversion_factor", "mg_equivalent",
]
SALES_COLS = [
    "org_id", "ndc", "drug_name", "data_source", "brand_flag",
    "pack_units", "total_mg", "wac", "transaction_date", "week_ending_date",
    "state", "specialty", "period_wk", "period_mo", "period_qtr",
    "wk_offset", "mo_offset",
]
ZIP_COLS = [
    "zip", "state", "territory_number", "territory_name",
    "region_number", "region_name",
]


class LoadError(RuntimeError):
    """Raised when the dataset cannot be safely published."""


def _truncate_business_data(cur: Any) -> None:
    cur.execute(
        "TRUNCATE sales, organizations, products, zip_territory, "
        "app_ref.product_classification RESTART IDENTITY CASCADE"
    )


def _copy_csv(cur: Any, table: str, columns: list[str], path: pathlib.Path) -> int:
    """Stream a CSV straight into PostgreSQL.

    NULL '' turns the generator's empty hierarchy fields into real NULLs rather
    than empty strings, which matters because COALESCE(grandparent_org_id, ...)
    must fall back for standalone facilities.
    """
    with path.open("r", newline="") as fh:
        header = next(csv.reader(fh))
        if header != columns:
            raise LoadError(
                f"{path.name} header does not match the expected contract.\n"
                f"  expected: {columns}\n  found:    {header}"
            )
        copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, NULL '')").format(
            sql.Identifier(*table.split(".")),
            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        )
        with cur.copy(copy_stmt) as copy:
            while chunk := fh.read(1 << 20):
                copy.write(chunk)
    cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
    return cur.fetchone()["n"]


def _load_full(cur: Any, report: LoadReport) -> None:
    files = {
        "organizations": (GENERATED / "organizations.csv", ORG_COLS),
        "products": (GENERATED / "products.csv", PRODUCT_COLS),
        "zip_territory": (GENERATED / "zip_territory.csv", ZIP_COLS),
        "sales": (GENERATED / "sales.csv", SALES_COLS),
    }
    missing = [str(p) for p, _ in files.values() if not p.exists()]
    if missing:
        raise LoadError(
            "generated CSVs are missing -- run `python3 schema/generate_data.py` first:\n  "
            + "\n  ".join(missing)
        )

    # Reference tables before sales, so the foreign keys hold at every point.
    for table in ("organizations", "products", "zip_territory", "sales"):
        path, cols = files[table]
        report.source_hashes[path.name] = file_sha256(path)
        report.row_counts[table] = _copy_csv(cur, table, cols, path)


def _load_seed(cur: Any, report: LoadReport) -> None:
    if not SEED_SQL.exists():
        raise LoadError(f"missing {SEED_SQL}")
    report.source_hashes[SEED_SQL.name] = file_sha256(SEED_SQL)
    statements = SEED_SQL.read_text()
    # The seed file also inserts users; those are handled by the users
    # bootstrap so both load modes share one identity path.
    cur.execute(statements)
    for table in BUSINESS_TABLES:
        cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
        report.row_counts[table] = cur.fetchone()["n"]


def _bootstrap_users(cur: Any, report: LoadReport) -> None:
    """Users come from the supplied seed SQL in BOTH modes.

    The generator emits no users CSV, and the users table is the authority for
    role, territory/region assignment and can_view_wac. Loading it separately
    keeps identity independent of which business dataset is active.
    """
    cur.execute("SELECT count(*) AS n FROM users")
    if cur.fetchone()["n"] > 0:
        report.row_counts["users"] = _count(cur, "users")
        return

    text = SEED_SQL.read_text()
    marker = "INSERT INTO users"
    idx = text.index(marker)
    end = text.index(";", idx) + 1
    cur.execute(text[idx:end])
    report.row_counts["users"] = _count(cur, "users")


def _count(cur: Any, table: str) -> int:
    cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
    return cur.fetchone()["n"]


def _populate_classification(cur: Any) -> None:
    cur.execute("SELECT ndc, drug_name, brand_flag FROM products")
    rows = cur.fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        classification, derivation = classify(row["drug_name"], row["brand_flag"])
        writer.writerow([row["ndc"], classification, derivation, RULE_VERSION])
    buf.seek(0)
    with cur.copy(
        "COPY app_ref.product_classification (ndc, classification, derivation, rule_version) "
        "FROM STDIN WITH (FORMAT csv)"
    ) as copy:
        copy.write(buf.read())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(cur: Any, report: LoadReport) -> None:
    fatal: list[str] = []

    # --- integrity that must hold for safe execution -----------------------
    cur.execute(
        "SELECT count(*) AS n FROM sales s "
        "LEFT JOIN organizations o ON o.org_id = s.org_id WHERE o.org_id IS NULL"
    )
    if (n := cur.fetchone()["n"]):
        fatal.append(f"{n} sales rows reference a missing organization")

    cur.execute(
        "SELECT count(*) AS n FROM sales s "
        "LEFT JOIN products p ON p.ndc = s.ndc WHERE p.ndc IS NULL"
    )
    if (n := cur.fetchone()["n"]):
        fatal.append(f"{n} sales rows reference a missing product")

    cur.execute("SELECT DISTINCT data_source FROM sales")
    unknown = {r["data_source"] for r in cur.fetchall()} - VALID_SOURCES
    if unknown:
        fatal.append(f"unknown data_source values: {sorted(unknown)}")

    if fatal:
        raise LoadError("; ".join(fatal))

    # --- conversion factor coverage (A2) ------------------------------------
    cur.execute(
        "SELECT count(*) AS n FROM products "
        "WHERE unit_conversion_factor IS NULL OR unit_conversion_factor <= 0"
    )
    n = cur.fetchone()["n"]
    if n:
        report.warn(
            "conversion_factor_missing",
            f"{n} products have a missing or non-positive unit_conversion_factor; "
            "their equivalents resolve to NULL rather than 0 or 1",
            products=n,
        )
    # Recorded whether or not it is zero, so a query-time warning can cite
    # measured evidence instead of asserting a property of the data. Without
    # this the ingestion report knew, and every answer built on equivalents
    # silently dropped those products and presented the result as a total.
    report.source_coverage["products_missing_conversion_factor"] = n

    # --- the two equivalents formulas disagree (A2) -------------------------
    cur.execute(
        """
        SELECT count(*) AS n
        FROM sales s JOIN products p ON p.ndc = s.ndc
        WHERE p.mg_equivalent > 0 AND p.unit_conversion_factor IS NOT NULL
          AND abs(s.pack_units * p.unit_conversion_factor
                  - s.total_mg / p.mg_equivalent) > 1e-9
        """
    )
    if (n := cur.fetchone()["n"]):
        report.warn(
            "equivalents_formula_conflict",
            f"the documented milligram formula disagrees with the conversion-factor "
            f"formula on {n} rows; the conversion-factor formula is authoritative",
            rows=n,
        )

    # --- market data contains no company brand (A3) -------------------------
    cur.execute(
        "SELECT count(*) AS n FROM sales WHERE data_source = 'market_data' AND brand_flag = 1"
    )
    company_in_market = cur.fetchone()["n"]
    if company_in_market == 0:
        report.warn(
            "market_data_competitor_only",
            "every market_data row has brand_flag = 0, so the denominator is a "
            "competitor-only total rather than the documented total market; "
            "market share may exceed 100% and is reported with that caveat",
        )
    report.source_coverage["market_data_company_rows"] = company_in_market

    # --- impossible ratios, measured per subcategory ------------------------
    cur.execute(
        """
        WITH num AS (
            SELECT p.market_subcategory AS sub,
                   sum(s.pack_units * p.unit_conversion_factor) AS v
            FROM sales s JOIN products p ON p.ndc = s.ndc
            WHERE s.data_source = 'distributor' AND s.brand_flag = 1
            GROUP BY 1
        ), den AS (
            SELECT p.market_subcategory AS sub,
                   sum(s.pack_units * p.unit_conversion_factor) AS v
            FROM sales s JOIN products p ON p.ndc = s.ndc
            WHERE s.data_source = 'market_data'
            GROUP BY 1
        )
        SELECT num.sub, num.v / den.v AS ratio
        FROM num JOIN den USING (sub)
        WHERE den.v > 0 AND num.v / den.v > 1.0
        ORDER BY ratio DESC
        """
    )
    impossible = [(r["sub"], round(r["ratio"], 6)) for r in cur.fetchall()]
    if impossible:
        report.warn(
            "market_share_exceeds_one",
            "all-time market share exceeds 100% in these subcategories, confirming "
            "the denominator is incomplete",
            subcategories={sub: ratio for sub, ratio in impossible},
        )

    # --- geography coverage (A4) --------------------------------------------
    cur.execute(
        "SELECT count(*) AS n FROM organizations o "
        "LEFT JOIN zip_territory z ON z.zip = o.zip WHERE z.zip IS NULL"
    )
    unmapped_orgs = cur.fetchone()["n"]
    cur.execute(
        "SELECT count(*) AS n FROM sales s JOIN organizations o ON o.org_id = s.org_id "
        "LEFT JOIN zip_territory z ON z.zip = o.zip WHERE z.zip IS NULL"
    )
    unmapped_sales = cur.fetchone()["n"]
    if unmapped_orgs:
        report.warn(
            "organizations_without_territory",
            f"{unmapped_orgs} organizations have a ZIP with no zip_territory mapping "
            f"({unmapped_sales} sales rows); scoped roles cannot see them (fail closed) "
            "and Exec totals label them as unmapped",
            organizations=unmapped_orgs, sales_rows=unmapped_sales,
        )
    report.source_coverage["organizations_without_territory"] = unmapped_orgs
    report.source_coverage["sales_rows_without_territory"] = unmapped_sales

    # --- users whose assignment matches no ZIP (A5) -------------------------
    cur.execute(
        """
        SELECT u.user_id, u.role, u.territory_name, u.region_name
        FROM users u
        WHERE (u.role = 'ram' AND NOT EXISTS (
                  SELECT 1 FROM zip_territory z WHERE z.territory_name = u.territory_name))
           OR (u.role = 'director' AND NOT EXISTS (
                  SELECT 1 FROM zip_territory z WHERE z.region_name = u.region_name))
        """
    )
    orphaned = cur.fetchall()
    if orphaned:
        report.warn(
            "user_assignment_unmatched",
            f"{len(orphaned)} users have an assignment that matches no zip_territory row; "
            "they authenticate but every scoped query correctly returns zero rows",
            users=[r["user_id"] for r in orphaned],
        )

    # --- reporting calendar (A6) --------------------------------------------
    cur.execute(
        "SELECT count(*) AS n FROM sales "
        "WHERE period_mo IS DISTINCT FROM to_char(transaction_date::date, 'YYYY-MM')"
    )
    if (n := cur.fetchone()["n"]):
        report.warn(
            "period_month_differs_from_transaction_month",
            f"{n} rows carry a period_mo different from their transaction month, because "
            "periods follow the week-ending month; this is a reporting-calendar "
            "convention and is surfaced rather than corrected",
            rows=n,
        )

    cur.execute(
        "SELECT DISTINCT trim(to_char(week_ending_date::date, 'Day')) AS dow FROM sales"
    )
    dows = sorted(r["dow"] for r in cur.fetchall())
    report.source_coverage["week_ending_weekdays"] = dows
    if dows and dows != ["Saturday"]:
        report.warn(
            "week_ending_not_saturday",
            f"week_ending_date falls on {', '.join(dows)}, though the supplied DDL and "
            "period_offsets.md both describe Saturday",
            weekdays=dows,
        )

    # --- duplicate organization names (A7) ----------------------------------
    cur.execute(
        "SELECT count(*) AS n FROM ("
        "  SELECT org_name FROM organizations GROUP BY org_name HAVING count(*) > 1"
        ") d"
    )
    if (n := cur.fetchone()["n"]):
        report.warn(
            "duplicate_organization_names",
            f"{n} organization names are shared by more than one org_id; all grouping "
            "uses stable ids and names are labels only",
            name_groups=n,
        )

    # --- reporting anchor, derived from data --------------------------------
    cur.execute(
        """
        SELECT min(mo_offset) AS min_mo, max(mo_offset) AS max_mo,
               min(wk_offset) AS min_wk, max(wk_offset) AS max_wk,
               max(transaction_date) AS max_txn,
               max(week_ending_date) AS max_week_ending,
               max(period_mo) AS max_period_mo,
               max(period_qtr) AS max_period_qtr
        FROM sales
        """
    )
    anchor = cur.fetchone()
    report.reporting_anchor = {k: v for k, v in anchor.items()}

    cur.execute("SELECT data_source, count(*) AS n FROM sales GROUP BY 1 ORDER BY 1")
    report.source_coverage["rows_by_source"] = {r["data_source"]: r["n"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load(mode: str) -> LoadReport:
    if mode not in ("seed", "full"):
        raise LoadError(f"unknown load mode {mode!r}")

    dataset_id = f"{mode}-{uuid.uuid4().hex[:12]}"
    report = LoadReport(dataset_id=dataset_id, load_mode=mode)

    with owner_transaction() as cur:
        cur.execute(
            "INSERT INTO app_meta.dataset_manifest "
            "(dataset_id, load_mode, load_state, schema_version, mapping_version, "
            " source_hashes, row_counts, reporting_anchor, source_coverage) "
            "VALUES (%s, %s, 'loading', %s, %s, '{}', '{}', '{}', '{}')",
            (dataset_id, mode, "1.0.0", MAPPING_VERSION),
        )

    try:
        # One transaction: either the whole snapshot lands or none of it does.
        with owner_transaction() as cur:
            # Before anything is truncated or written: refuse a database whose
            # shape this system cannot answer questions about. Failing here is
            # far better than failing mid-query, or -- worse -- succeeding
            # against columns that happen to still exist.
            schema = require_compatible(cur)
            report.source_coverage["schema_fingerprint"] = schema.fingerprint
            report.source_coverage["schema_contract_version"] = CONTRACT_VERSION
            if schema.extra_columns:
                report.warn(
                    "schema_has_extra_columns",
                    f"{len(schema.extra_columns)} column(s) outside the contract are "
                    "present and ignored; this does not affect any answer",
                    columns=sorted(schema.extra_columns),
                )
            _truncate_business_data(cur)
            if mode == "full":
                _load_full(cur, report)
            else:
                _load_seed(cur, report)
            _bootstrap_users(cur, report)
            _populate_classification(cur)
            cur.execute("ANALYZE sales")
            cur.execute("ANALYZE organizations")
            _validate(cur, report)
    except Exception as exc:
        with owner_transaction() as cur:
            cur.execute(
                "UPDATE app_meta.dataset_manifest SET load_state = 'failed', "
                "warnings = %s::jsonb WHERE dataset_id = %s",
                (_json([{"code": "load_failed", "message": str(exc)}]), dataset_id),
            )
        raise

    # Publish only after validation passed, and supersede the previous snapshot
    # in the same transaction so there is never more than one published dataset.
    with owner_transaction() as cur:
        cur.execute(
            "UPDATE app_meta.dataset_manifest SET load_state = 'superseded' "
            "WHERE load_state = 'published'"
        )
        cur.execute(
            "UPDATE app_meta.dataset_manifest SET load_state = 'published', "
            "published_at = now(), source_hashes = %s::jsonb, row_counts = %s::jsonb, "
            "reporting_anchor = %s::jsonb, source_coverage = %s::jsonb, warnings = %s::jsonb "
            "WHERE dataset_id = %s",
            (
                _json(report.source_hashes), _json(report.row_counts),
                _json(report.reporting_anchor), _json(report.source_coverage),
                _json(report.warnings), dataset_id,
            ),
        )
    return report


def _json(value: Any) -> str:
    import json
    return json.dumps(value, default=str)
