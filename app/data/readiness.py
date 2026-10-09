"""Readiness of a loaded source, section by section, from what was measured.

Answers "could this system answer questions about this data, and keep it
current?" for one published dataset: its identifiers, periods, source
totals and overlap, units, money, classifications, hierarchy, territory
access, cadence, and correction and deletion identity, plus the contract
thresholds in force. Every section is computed from the published manifest
and the database (never from a document), and carries its evidence.

Statuses:

* ``ready``        -- measured, nothing to resolve;
* ``attention``    -- measured; answers carry a caveat, or an owner must confirm
                      an interpretation the data cannot settle;
* ``blocked``      -- measured; something the data must change before it is
                      used for that purpose (e.g. ingestion cannot extend it);
* ``not_measured`` -- nothing here exercises it (e.g. no batch was ingested).

A report on synthetic data is evidence about the system's handling of that
shape of data, never about a real feed: ``real_data`` says which it is, and
is ``None`` when the caller does not know.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.data import sources

REPORT_VERSION = "1.0.0"

#: Contract limits in force. None was set from a measured feed: each is an
#: engineering bound chosen so that no plausible sale reaches it, and each is
#: provisional until a real feed's distribution is known (docs/INGESTION.md).
def thresholds() -> dict[str, Any]:
    settings = get_settings()
    return {
        "max_packs_per_event": {"value": sources.MAX_PACKS_PER_EVENT, "provisional": True},
        "max_wac_per_pack": {"value": sources.MAX_WAC_PER_PACK, "provisional": True},
        "max_events_per_batch": {"value": sources.MAX_EVENTS_PER_BATCH, "provisional": True},
        "max_identity_length": {"value": sources.MAX_IDENTITY_LENGTH, "provisional": True},
        "ingest_max_quarantine_ratio": {"value": settings.ingest_max_quarantine_ratio,
                                        "provisional": True},
        "business_timezone": {"value": settings.business_timezone, "provisional": True},
    }


def _one(cur: Any, sql: str, params: tuple = ()) -> dict[str, Any]:
    cur.execute(sql, params)
    return dict(cur.fetchone())


def _all(cur: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _section(status: str, evidence: dict[str, Any], *notes: str) -> dict[str, Any]:
    return {"status": status, "evidence": evidence, "notes": list(notes)}


def build(cur: Any, *, real_data: bool | None = None,
          onboarding: dict[str, Any] | None = None) -> dict[str, Any]:
    """The report for the published dataset, read through an owner cursor."""
    manifest = _one(cur, "SELECT dataset_id, load_mode, reporting_anchor, source_coverage, "
                         "warnings, row_counts, source_hashes FROM app_meta.dataset_manifest "
                         "WHERE load_state = 'published'")
    coverage = manifest["source_coverage"] or {}
    warnings = {w["code"]: w for w in (manifest["warnings"] or [])}
    sections: dict[str, dict[str, Any]] = {}

    # -- identifiers ---------------------------------------------------------
    products = _one(cur, "SELECT count(*) AS products, count(DISTINCT ndc) AS ndcs, "
                         "count(*) FILTER (WHERE ndc !~ '^[0-9]{4,5}-[0-9]{3,4}-[0-9]{1,2}$') "
                         "AS ndc_not_in_10_digit_form FROM products")
    labels = _one(cur, "SELECT count(*) AS n FROM (SELECT drug_name FROM products GROUP BY 1 "
                       "HAVING count(*) > 1) x")
    orgs = _one(cur, "SELECT count(*) AS organizations FROM organizations")
    sections["identifiers"] = _section(
        "attention" if products["ndc_not_in_10_digit_form"] else "ready",
        {**products, **orgs, "drug_names_over_several_ndcs": labels["n"],
         "organization_names_shared_by_several_ids":
             warnings.get("duplicate_organization_names", {}).get("name_groups", 0)},
        "Grouping uses ndc and org_id; names are labels only.")

    # -- periods ---------------------------------------------------------------
    calendar = coverage.get("calendar")
    span = _one(cur, "SELECT count(*) AS weeks, min(week_ending_date) AS first_week_ending, "
                     "max(week_ending_date) AS last_week_ending FROM app_ref.calendar")
    gaps = [code for code in warnings if code.startswith("calendar_gap")]
    if calendar is None:
        status, note = "not_measured", "The manifest predates the calendar check."
    elif not calendar["extendable"]:
        status, note = "blocked", f"Ingestion cannot add a week: {calendar['reason']}"
    else:
        status = "attention" if gaps else "ready"
        note = "Ingestion can add weeks by the convention recorded here."
    sections["periods"] = _section(
        status, {**span, "calendar": calendar, "reporting_anchor": manifest["reporting_anchor"],
                 "business_timezone": get_settings().business_timezone, "gap_warnings": gaps},
        note, "Event timestamps must carry an offset; a sale's day is taken in the business "
              "timezone.")

    # -- totals and overlap between sources ------------------------------------
    by_source = _all(cur, "SELECT data_source, count(*) AS rows, sum(pack_units) AS packs "
                          "FROM sales GROUP BY 1 ORDER BY 1")
    # A month lacks a source if any of its weeks does: a metric reading that
    # source reports the month as unknown (app/analytics/compiler.py).
    every = {r["data_source"] for r in by_source}
    in_all_weeks: dict[str, set[str]] = {}
    for r in _all(cur, "SELECT period_mo, sources FROM app_ref.calendar ORDER BY 1"):
        in_all_weeks[r["period_mo"]] = in_all_weeks.get(r["period_mo"], every) & set(r["sources"])
    months_missing_a_source = {m: sorted(every - present)
                               for m, present in sorted(in_all_weeks.items()) if present != every}
    company_in_market = coverage.get("market_data_company_rows")
    notes = []
    if company_in_market == 0:
        notes.append("Market data holds no company rows, so market share divides by a "
                      "competitor-only total; answers say so.")
    if months_missing_a_source:
        notes.append("Some months lack a source; those months are unknown, not zero, for "
                     "metrics that read it.")
    sections["totals_and_overlap"] = _section(
        "attention" if notes else "ready",
        {"by_source": [{**r, "packs": float(r["packs"] or 0)} for r in by_source],
         "market_data_company_rows": company_in_market,
         "months_missing_a_source": months_missing_a_source,
         "market_share_exceeds_one": "market_share_exceeds_one" in warnings},
        *notes)

    # -- units -----------------------------------------------------------------
    missing_factor = coverage.get("products_missing_conversion_factor")
    sections["units"] = _section(
        "attention" if missing_factor else "ready" if missing_factor == 0 else "not_measured",
        {"products_missing_conversion_factor": missing_factor,
         "equivalents_formula_conflict_rows":
             warnings.get("equivalents_formula_conflict", {}).get("rows")},
        "Volume is pack_units; equivalents are pack_units x unit_conversion_factor (A2).")

    # -- money -----------------------------------------------------------------
    money = _one(cur, "SELECT count(*) FILTER (WHERE data_source = 'distributor') AS distributor_rows, "
                      "count(*) FILTER (WHERE data_source = 'distributor' AND wac > 0) AS priced, "
                      "count(*) FILTER (WHERE data_source <> 'distributor' AND wac <> 0) "
                      "AS priced_outside_distributor, count(*) FILTER (WHERE wac < 0) AS negative "
                      "FROM sales")
    # Per NDC: one drug name over two strengths has two prices.
    spread = _all(cur, "SELECT ndc, percentile_cont(0.1) WITHIN GROUP (ORDER BY wac / pack_units) "
                       "AS p10, percentile_cont(0.9) WITHIN GROUP (ORDER BY wac / pack_units) AS p90 "
                       "FROM sales WHERE data_source = 'distributor' AND pack_units > 0 AND wac > 0 "
                       "GROUP BY 1 ORDER BY 1")
    sections["money"] = _section(
        "attention",
        {**money, "wac_per_pack_by_ndc": [
            {"ndc": r["ndc"], "p10": round(float(r["p10"]), 2),
             "p90": round(float(r["p90"]), 2)} for r in spread]},
        "WAC is read as the transaction's dollar amount, not a price per pack (A8). A narrow "
        "WAC-per-pack band within an NDC is consistent with that reading but cannot prove it; "
        "the data owner must confirm.")

    # -- classifications ---------------------------------------------------------
    classes = coverage.get("classification")
    if classes is None:
        status, note = "not_measured", "The manifest predates classification authority."
    elif not classes["mappings"]:
        status, note = "attention", ("No classification mapping: only the company's own "
                                     "products have a class; segment shares are ranges.")
    elif classes["unknown_products"]:
        status, note = "attention", (f"{classes['unknown_products']} product(s) of unknown "
                                     "class; segment shares carry their volume as a range.")
    else:
        status, note = "ready", "Every product is classified by the source or the mapping."
    sections["classifications"] = _section(status, {"classification": classes}, note)

    # -- hierarchy ---------------------------------------------------------------
    hierarchy = _one(cur, "SELECT count(*) FILTER (WHERE org_type = 'Facility') AS facilities, "
                          "count(*) FILTER (WHERE org_type = 'Facility' AND parent_org_id IS NULL) "
                          "AS standalone_facilities, count(*) FILTER (WHERE org_type = 'Facility' "
                          "AND parent_org_id IS NOT NULL AND grandparent_org_id IS NULL) "
                          "AS facilities_without_grandparent, count(*) FILTER "
                          "(WHERE org_status <> 'Active') AS inactive FROM organizations")
    unmapped = coverage.get("organizations_without_territory")
    sections["hierarchy"] = _section(
        "attention" if unmapped else "ready",
        {**hierarchy, "organizations_without_territory": unmapped,
         "sales_rows_without_territory": coverage.get("sales_rows_without_territory")},
        "Accounts roll up to the top organization present; a facility outside every "
        "territory is visible only to Exec totals, labelled unmapped.")

    # -- territory access ----------------------------------------------------------
    roles = _all(cur, "SELECT role, count(*) AS users FROM users GROUP BY 1 ORDER BY 1")
    empty = _all(cur, "SELECT u.user_id, u.role FROM users u WHERE "
                      "(u.role = 'ram' AND NOT EXISTS (SELECT 1 FROM zip_territory z "
                      "WHERE z.territory_name = u.territory_name)) OR (u.role = 'director' "
                      "AND NOT EXISTS (SELECT 1 FROM zip_territory z WHERE z.region_name = "
                      "u.region_name)) ORDER BY 1")
    unassigned = _all(cur, "SELECT DISTINCT territory_name FROM zip_territory z WHERE NOT EXISTS "
                           "(SELECT 1 FROM users u WHERE u.territory_name = z.territory_name) "
                           "ORDER BY 1")
    sections["territory_access"] = _section(
        "attention" if empty else "ready",
        {"users_by_role": roles, "scoped_users_with_empty_scope": [r["user_id"] for r in empty],
         "territories_without_a_user": [r["territory_name"] for r in unassigned]},
        "Scope binds by territory and region name; each name is unique (enforced at load). "
        "A scoped user whose name matches no territory sees nothing.")

    # -- cadence -------------------------------------------------------------------
    batches = _all(cur, "SELECT source_system, status, count(*) AS batches, "
                        "max(last_attempt_at) AS last_attempt FROM app_ingest.batches "
                        "GROUP BY 1, 2 ORDER BY 1, 2")
    marks = _all(cur, "SELECT source_system, watermark, last_batch_at FROM app_ingest.watermarks "
                      "ORDER BY 1")
    sections["cadence"] = _section(
        "ready" if marks else "not_measured",
        {"batches": batches, "watermarks": marks},
        "Synthetic batches establish handling, not a feed's real cadence or lateness."
        if marks else "No batch has been ingested into this dataset.")

    # -- correction and deletion identity ---------------------------------------------
    ledger = _all(cur, "SELECT source_system, count(*) AS events, count(*) FILTER "
                       "(WHERE event_version > 1) AS corrected, count(*) FILTER "
                       "(WHERE tombstoned) AS tombstoned FROM app_ingest.event_ledger "
                       "GROUP BY 1 ORDER BY 1")
    quarantine = _all(cur, "SELECT reason, count(*) AS events FROM app_ingest.quarantine "
                           "GROUP BY 1 ORDER BY 1")
    rejected = _all(cur, "SELECT rejection_code, count(*) AS batches FROM app_ingest.batches "
                         "WHERE status = 'rejected' GROUP BY 1 ORDER BY 1")
    reconciled = (onboarding or {}).get("reconciliation")
    if not ledger:
        status = "not_measured"
    elif reconciled is not None and not (reconciled.get("every_event_reconciled")
                                         and reconciled.get("totals_reconcile")):
        status = "blocked"
    else:
        status = "ready"
    sections["correction_identity"] = _section(
        status,
        {"ledger": ledger, "quarantine_by_reason": quarantine, "rejected_by_code": rejected,
         "onboarding_reconciliation": reconciled},
        "Each event is identified by (source_system, source_event_id) with a version; a "
        "correction is a higher version, a deletion a tombstone. Without stable identity "
        "from the feed, neither can be applied.")

    statuses = [s["status"] for s in sections.values()]
    return {
        "report_version": REPORT_VERSION,
        "dataset_id": manifest["dataset_id"], "load_mode": manifest["load_mode"],
        "real_data": real_data,
        "overall": ("blocked" if "blocked" in statuses else
                    "attention" if "attention" in statuses else "ready"),
        "sections": sections,
        "thresholds": thresholds(),
        "row_counts": manifest["row_counts"],
        "source_hashes": manifest["source_hashes"],
    }
