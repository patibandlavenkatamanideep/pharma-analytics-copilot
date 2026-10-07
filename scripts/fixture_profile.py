#!/usr/bin/env python3
"""Same-schema fixture profiles that look nothing like the supplied dataset.

    python3 scripts/fixture_profile.py --profile orchard --out <dir>

Every number the system is checked against elsewhere comes from one generated
dataset with one set of names, one geography, one skew and complete sources. A
system tuned to that dataset can pass every test and still be wrong on a
customer's. A profile keeps the supplied table definitions and changes
everything else, deterministically from its seed:

* unfamiliar product, organization and territory names and identifier formats;
* duplicated labels (two facilities with one name; one drug name over two
  strengths);
* a different cardinality and a heavy skew (one account holding most volume);
* source omissions (market data absent for whole months) and a covered month
  in which one product sold nothing;
* products whose true classification is unknown -- not a generic, biosimilar or
  company brand by any documented rule;
* a hierarchy with facilities under parents without a grandparent, standalone
  facilities, an unmapped ZIP and an inactive facility;
* an ingestion series with replays, equal-valued distinct events, a correction,
  a tombstone, a tombstone that arrives before its sale, a conflicting
  version, a late event, records the contract refuses (negative packs, a
  missing identity, an unknown organization), a batch rejected whole, and a
  sale in a week after the base data, which moves the reporting anchor.

Writes organizations.csv, products.csv, zip_territory.csv, sales.csv (the
loader's inputs), users.json, batches/*.json (ingestion), and manifest.json:
the profile's parameters, seed, generator and contract versions, the expected
classification of each product, the expected outcome of every ingestion event,
and each file's SHA-256. Nothing here is a real customer's data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import random
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR_VERSION = "1.0.0"

ORG_FIELDS = ["org_id", "org_name", "org_type", "org_status", "org_archetype", "specialty",
              "address_line1", "city", "state", "zip", "parent_org_id", "parent_org_name",
              "grandparent_org_id", "grandparent_org_name", "gpo_name", "is_340b"]
PRODUCT_FIELDS = ["ndc", "drug_name", "generic_name", "strength", "form", "brand_flag",
                  "specialty", "market_category", "market_subcategory",
                  "unit_conversion_factor", "mg_equivalent"]
SALES_FIELDS = ["org_id", "ndc", "drug_name", "data_source", "brand_flag", "pack_units",
                "total_mg", "wac", "transaction_date", "week_ending_date", "state", "specialty",
                "period_wk", "period_mo", "period_qtr", "wk_offset", "mo_offset"]
ZIP_FIELDS = ["zip", "state", "territory_number", "territory_name", "region_number",
              "region_name"]

#: A product: (ndc, drug_name, generic_name, strength, brand_flag, category, subcategory,
#: unit_conversion_factor, mg_equivalent, price per pack, TRUE classification)
PROFILES: dict[str, dict] = {
    "orchard": {
        "description": "Unfamiliar names, a dominant account, market data missing for two "
                       "months, unknown classifications, an anchor whose history crosses an ISO week-53 year end",
        "seed": 20261007,
        "anchor_saturday": "2021-03-20",
        "weeks": 26,
        "rows_per_week": 140,
        "regions": [("RX1", "Interior"), ("RX2", "Littoral")],
        # (number, name, region, state, zip prefix)
        "territories": [("TA-01", "Basin North", "RX1", "MT", "591"),
                        ("TA-02", "Basin South", "RX1", "WY", "820"),
                        ("TB-01", "Coastal Arc", "RX2", "OR", "973"),
                        # Territory names must be unique: scope binds by name. A
                        # duplicate is refused by the loader (see the test).
                        ("TB-02", "Spruce Reach", "RX2", "WA", "981")],
        "systems": [("SYS-MRD", "Meridian Care Network", 3, 4),
                    ("SYS-OKH", "Oakhollow Health", 2, 3),
                    ("SYS-TIN", "Tinder Valley Partners", 1, 3)],
        "standalone": 6,
        "parent_without_grandparent": 2,
        "dominant_share": 0.62,
        "share_340b": 0.2,
        "products": [
            ("70707-0010-01", "VELTRIQ", "veltrisab", "10MG/2ML", 1, "Immunology", "Anti-IL",
             1.0, 10.0, 950.0, "company_brand"),
            ("70707-0002-01", "VELTRIQ", "veltrisab", "2MG/1ML", 1, "Immunology", "Anti-IL",
             0.2, 2.0, 210.0, "company_brand"),
            ("70707-0300-01", "OMBRAZEN", "ombrazumab", "300MG", 1, "Immunology", "Anti-TNF",
             1.0, 300.0, 4200.0, "company_brand"),
            ("81818-0010-01", "KESTALIN", "kestaprin", "10MG/2ML", 0, "Immunology", "Anti-IL",
             1.0, 10.0, 0.0, "branded_competitor"),
            ("81818-0011-01", "VELTRISAB GENERIC", "veltrisab", "10MG/2ML", 0, "Immunology",
             "Anti-IL", 1.0, 10.0, 0.0, "generic"),
            ("81818-0301-01", "OMBRAZUMAB BIOSIMILAR", "ombrazumab", "300MG", 0, "Immunology",
             "Anti-TNF", 1.0, 300.0, 0.0, "biosimilar"),
            # Sold as an unbranded generic under a brand-like trade name: no suffix,
            # brand_flag 0. Its true class is unknown to anything in the data.
            ("81818-0012-01", "PRAXOLONE", "veltrisab", "10MG/2ML", 0, "Immunology", "Anti-IL",
             1.0, 10.0, 0.0, "unknown"),
            ("81818-0302-01", "TUMNEXA", "ombrazumab", "300MG", 0, "Immunology", "Anti-TNF",
             1.0, 300.0, 0.0, "unknown"),
            # A market the company does not compete in.
            ("92929-0050-01", "GLORIBAN GENERIC", "gloribam", "50MG", 0, "Dermatology",
             "Topical", 1.0, 50.0, 0.0, "generic"),
            ("92929-0051-01", "GLORIBAN", "gloribam", "50MG", 0, "Dermatology", "Topical",
             1.0, 50.0, 0.0, "branded_competitor"),
        ],
        # market data absent in these weeks (whole months: no market coverage)
        "market_gap_weeks": list(range(9, 18)),
        # a covered month in which this product sold nothing (distributor)
        "silent_product": ("70707-0300-01", list(range(4, 9))),
        "hub_products": ["70707-0010-01"],
    },
    "estuary": {
        "description": "Many small accounts, more territories, no 340B facilities, no hub "
                       "data, a short history ending mid-year",
        "seed": 31415926,
        "anchor_saturday": "2026-06-27",
        "weeks": 13,
        "rows_per_week": 220,
        "regions": [("E-N", "Northshore"), ("E-S", "Southreach"), ("E-W", "Westmere")],
        "territories": [("E01", "Firth", "E-N", "ME", "040"), ("E02", "Kyle", "E-N", "NH", "033"),
                        ("E03", "Sound", "E-S", "NC", "275"), ("E04", "Delta", "E-S", "LA", "701"),
                        ("E05", "Mere", "E-W", "NV", "891"), ("E06", "Strand", "E-W", "AZ", "850")],
        "systems": [("IDN-001", "Kittiwake Health", 4, 5), ("IDN-002", "Curlew Medical", 3, 4)],
        "standalone": 40,
        "parent_without_grandparent": 3,
        "dominant_share": 0.0,
        "share_340b": 0.0,
        "products": [
            ("55555-0100-01", "SOLUNAR", "solunarin", "100MG", 1, "Oncology", "Kinase",
             1.0, 100.0, 1800.0, "company_brand"),
            ("55555-0025-01", "SOLUNAR", "solunarin", "25MG", 1, "Oncology", "Kinase",
             0.25, 25.0, 470.0, "company_brand"),
            ("66666-0100-01", "SOLUNARIN GENERIC", "solunarin", "100MG", 0, "Oncology", "Kinase",
             1.0, 100.0, 0.0, "generic"),
            ("66666-0101-01", "ARBORIX", "arborinib", "100MG", 0, "Oncology", "Kinase",
             1.0, 100.0, 0.0, "branded_competitor"),
            ("66666-0102-01", "QUENTRA", "solunarin", "100MG", 0, "Oncology", "Kinase",
             1.0, 100.0, 0.0, "unknown"),
        ],
        "market_gap_weeks": [4, 5, 6, 7],
        "silent_product": ("55555-0025-01", [8, 9, 10, 11]),
        "hub_products": [],
    },
}


def weeks_for(anchor: str, count: int) -> list[dict]:
    """Saturday week endings, the month and quarter of the Saturday, offsets
    from the anchor -- as schema/generate_data.py -- and the ISO week label,
    ISO year included. schema/generate_data.py labels a week with the
    Saturday's CALENDAR year and its ISO week number, which is wrong in the
    week that straddles a year end (Saturday 2027-01-02 is 2026-W53, not
    2027-W53). The supplied data never reaches such a week; a profile that
    does exposed that ingestion then refuses to extend the calendar
    (docs/QUALIFICATION_2026_10_07.md, step 3)."""
    base = datetime.strptime(anchor, "%Y-%m-%d")
    out = []
    for wk in range(count):
        sat = base - timedelta(weeks=wk)
        out.append({"wk_offset": wk,
                    "mo_offset": (base.year * 12 + base.month) - (sat.year * 12 + sat.month),
                    "week_start": sat - timedelta(days=5),
                    "week_ending_date": sat.strftime("%Y-%m-%d"),
                    "period_wk": f"{sat.isocalendar()[0]}-W{sat.isocalendar()[1]:02d}",
                    "period_mo": sat.strftime("%Y-%m"),
                    "period_qtr": f"{sat.year}-Q{(sat.month - 1) // 3 + 1}"})
    return out


def organizations(p: dict, rng: random.Random) -> tuple[list[dict], list[dict]]:
    orgs, zips = [], []
    zip_of: dict[str, list[str]] = {}
    for number, name, region, state, prefix in p["territories"]:
        region_name = dict(p["regions"])[region]
        for k in range(3):
            z = f"{prefix}{k:02d}"
            zips.append({"zip": z, "state": state, "territory_number": number,
                         "territory_name": name, "region_number": region,
                         "region_name": region_name})
            zip_of.setdefault(number, []).append(z)
    state_of = {z["zip"]: z["state"] for z in zips}
    territories = [t[0] for t in p["territories"]]

    def facility(org_id, name, z, parent=None, grand=None, status="Active", is_340b=0):
        return {"org_id": org_id, "org_name": name, "org_type": "Facility", "org_status": status,
                "org_archetype": rng.choice(["Hospital", "Clinic", "Infusion Center"]),
                "specialty": "Oncology", "address_line1": f"{rng.randint(1, 999)} Field Rd",
                "city": "Fixture", "state": state_of.get(z, "ZZ"), "zip": z,
                "parent_org_id": parent[0] if parent else "", "parent_org_name": parent[1] if parent else "",
                "grandparent_org_id": grand[0] if grand else "",
                "grandparent_org_name": grand[1] if grand else "",
                "gpo_name": rng.choice(["Cobalt GPO", "Ferrous Alliance", ""]), "is_340b": is_340b}

    n = 0
    for sys_id, sys_name, parents, per_parent in p["systems"]:
        orgs.append({**facility(sys_id, sys_name, zip_of[territories[0]][0]), "org_type": "Grandparent"})
        for i in range(parents):
            parent = (f"{sys_id}-P{i + 1}", f"{sys_name} Region {i + 1}")
            orgs.append({**facility(parent[0], parent[1], zip_of[territories[0]][0],
                                    grand=None), "org_type": "Parent",
                         "grandparent_org_id": sys_id, "grandparent_org_name": sys_name})
            for j in range(per_parent):
                n += 1
                t = territories[(n - 1) % len(territories)]
                orgs.append(facility(f"{sys_id}-F{i + 1}{j + 1}", f"{sys_name} Site {n}",
                                     zip_of[t][j % 3], parent=parent, grand=(sys_id, sys_name),
                                     is_340b=int(rng.random() < p["share_340b"])))
    for i in range(p["parent_without_grandparent"]):
        parent = (f"LONEP-{i + 1}", f"Lone Parent {i + 1}")
        orgs.append({**facility(parent[0], parent[1], zip_of[territories[-1]][0]), "org_type": "Parent"})
        t = territories[i % len(territories)]
        orgs.append(facility(f"LONEP-{i + 1}-F1", f"Lone Parent {i + 1} Site", zip_of[t][1],
                             parent=parent))
    for i in range(p["standalone"]):
        t = territories[i % len(territories)]
        # Two standalone facilities share one name, in different territories.
        name = "Harbor Infusion" if i < 2 else f"Independent Site {i + 1}"
        orgs.append(facility(f"IND-{i + 1:04d}", name, zip_of[t][i % 3],
                             is_340b=int(rng.random() < p["share_340b"])))
    orgs.append(facility("IND-UNMAPPED", "Unmapped Site", "00000"))
    orgs.append(facility("IND-CLOSED", "Closed Site", zip_of[territories[0]][2], status="Inactive"))
    return orgs, zips


def sales(p: dict, orgs: list[dict], rng: random.Random, weeks: list[dict]) -> list[dict]:
    facilities = [o for o in orgs if o["org_type"] == "Facility" and o["org_status"] == "Active"]
    dominant_system = p["systems"][0][0]
    if p["dominant_share"]:
        dominant = [f for f in facilities if f["grandparent_org_id"] == dominant_system]
        others = [f for f in facilities if f not in dominant]
        weights = [p["dominant_share"] / len(dominant) if f in dominant
                   else (1 - p["dominant_share"]) / len(others) for f in facilities]
    else:
        weights = [1.0] * len(facilities)
    products = p["products"]
    company = [x for x in products if x[4] == 1]
    gap = set(p["market_gap_weeks"])
    silent_ndc, silent_weeks = p["silent_product"] or (None, [])
    rows = []
    for week in weeks:
        for _ in range(p["rows_per_week"]):
            org = rng.choices(facilities, weights=weights)[0]
            roll = rng.random()
            if roll < 0.5:
                source, prod = "distributor", rng.choice(company)
                if prod[0] == silent_ndc and week["wk_offset"] in silent_weeks:
                    continue
            elif roll < 0.55 and p["hub_products"]:
                source = "hub_dispense"
                prod = next(x for x in products if x[0] == rng.choice(p["hub_products"]))
            else:
                if week["wk_offset"] in gap:
                    continue
                source, prod = "market_data", rng.choice(products)
            packs = float(rng.randint(1, 30 if org["grandparent_org_id"] == dominant_system else 8))
            wac = round(packs * prod[9] * rng.uniform(0.95, 1.05), 2) if source == "distributor" else 0.0
            txn = week["week_start"] + timedelta(days=rng.randint(0, 4))
            rows.append({"org_id": org["org_id"], "ndc": prod[0], "drug_name": prod[1],
                         "data_source": source, "brand_flag": prod[4], "pack_units": packs,
                         "total_mg": round(packs * prod[8], 2), "wac": wac,
                         "transaction_date": txn.strftime("%Y-%m-%d"),
                         "week_ending_date": week["week_ending_date"], "state": org["state"],
                         "specialty": "Oncology", "period_wk": week["period_wk"],
                         "period_mo": week["period_mo"], "period_qtr": week["period_qtr"],
                         "wk_offset": week["wk_offset"], "mo_offset": week["mo_offset"]})
    return rows


def users(p: dict, sales_rows: list[dict], orgs: list[dict], zips: list[dict]) -> list[dict]:
    """Every scoped user is assigned a territory or region that has sales, so a
    denial or a scoped total is never vacuously empty."""
    zip_territory = {z["zip"]: (z["territory_name"], z["region_name"]) for z in zips}
    org_zip = {o["org_id"]: o["zip"] for o in orgs}
    with_sales = {zip_territory[org_zip[r["org_id"]]] for r in sales_rows
                  if r["data_source"] == "distributor" and org_zip[r["org_id"]] in zip_territory}
    territory, region = sorted(with_sales)[0]
    regions = sorted({r for _, r in with_sales})
    return [
        {"user_id": "PX01", "email": "exec.wac@fixture.invalid", "full_name": "Fixture Exec",
         "role": "exec", "territory_name": None, "region_name": None, "can_view_wac": 1},
        {"user_id": "PX02", "email": "exec.nowac@fixture.invalid", "full_name": "Fixture Exec Two",
         "role": "exec", "territory_name": None, "region_name": None, "can_view_wac": 0},
        {"user_id": "PX03", "email": "director@fixture.invalid", "full_name": "Fixture Director",
         "role": "director", "territory_name": None, "region_name": regions[0], "can_view_wac": 0},
        {"user_id": "PX04", "email": "ram@fixture.invalid", "full_name": "Fixture RAM",
         "role": "ram", "territory_name": territory, "region_name": region, "can_view_wac": 0},
    ]


def batches(name: str, p: dict, orgs: list[dict], weeks: list[dict]) -> tuple[list[dict], list[dict]]:
    """The ingestion series and the outcome each event must have."""
    source = f"{name}-feed"
    facilities = [o for o in orgs if o["org_type"] == "Facility" and o["org_status"] == "Active"
                  and o["zip"] != "00000"]
    company = [x for x in p["products"] if x[4] == 1]
    latest = datetime.strptime(weeks[0]["week_ending_date"], "%Y-%m-%d")

    def at(week_offset: int, hour: int = 14) -> str:
        day = latest - timedelta(weeks=week_offset, days=2)
        return day.replace(hour=hour, tzinfo=timezone(timedelta(hours=-5))).isoformat()

    def event(sid, version=1, kind="upsert", org=None, prod=None, packs=10.0, week=0):
        org = org or facilities[0]
        prod = prod or company[0]
        return {"source_event_id": sid, "event_version": version, "kind": kind,
                "event_time": at(week), "org_id": org["org_id"], "ndc": prod[0],
                "data_source": "distributor", "pack_units": packs, "unit": "packs",
                "wac": round(prod[9], 2)}

    expected: list[dict] = []

    def keep(e, batch_id, outcome, delta):
        expected.append({"batch_id": batch_id, "source_event_id": e["source_event_id"],
                         "event_version": e["event_version"], "outcome": outcome,
                         "packs_delta": delta})
        return e

    b1_events = []
    for i in range(60):
        e = event(f"{name}-e{i:03d}", org=facilities[i % len(facilities)],
                  prod=company[i % len(company)], packs=float(5 + i % 7),
                  week=0 if i % 10 else 3)   # every tenth is a late event, three weeks back
        b1_events.append(keep(e, "b1", "insert", e["pack_units"]))
    # Two legitimately distinct sales with identical values.
    twin = dict(b1_events[1]); twin["source_event_id"] = f"{name}-twin"
    b1_events.append(keep(twin, "b1", "insert", twin["pack_units"]))
    bad = [dict(event(f"{name}-neg"), pack_units=-3.0),
           dict(event(""), source_event_id=""),
           dict(event(f"{name}-noorg"), org_id="NO-SUCH-ORG")]
    for e, reason in zip(bad, ("non_positive_packs", "invalid_identity", "unknown_organization")):
        b1_events.append(keep(e, "b1", f"quarantined:{reason}", 0.0))

    first, deleted = b1_events[2], b1_events[3]
    b2_events = [
        keep(dict(first, event_version=2, pack_units=first["pack_units"] + 4), "b2", "correction", 4.0),
        keep(dict(b1_events[0]), "b2", "duplicate", 0.0),
        keep(dict(deleted, event_version=2, kind="delete"), "b2", "tombstone", -deleted["pack_units"]),
        keep(event(f"{name}-early-delete", version=2, kind="delete"), "b2", "tombstone_without_sale", 0.0),
    ]
    b3_events = [keep(event(f"{name}-early-delete", version=1, packs=9.0), "b3", "duplicate", 0.0)]
    b4 = {"note": "declared_count is wrong: the whole batch is rejected and nothing applies"}
    b4_events = [event(f"{name}-rejected-{i}") for i in range(3)]
    for e in b4_events:
        keep(e, "b4", "rejected:control_totals", 0.0)
    b5_events = []
    for i in range(25):
        e = event(f"{name}-f{i:03d}", org=facilities[(i * 3) % len(facilities)], packs=4.0,
                  week=-1 if i == 0 else 0)  # the first falls in the week after the base data
        b5_events.append(keep(e, "b5", "insert", 4.0))
    conflict = dict(b1_events[5], pack_units=b1_events[5]["pack_units"] + 1)
    b5_events.append(keep(conflict, "b5", "quarantined:conflicting_versions", 0.0))

    def doc(batch_id, events, count=None):
        total = sum(e["pack_units"] for e in events)
        return {"source_system": source, "batch_id": f"{name}-{batch_id}",
                "declared_count": len(events) if count is None else count,
                "declared_pack_units": max(total, 0.0), "events": events}

    docs = [doc("b1", b1_events), doc("b2", b2_events), doc("b3", b3_events),
            doc("b4", b4_events, count=len(b4_events) + 1), doc("b5", b5_events)]
    del b4
    return docs, expected


def write(out: pathlib.Path, profile: str) -> dict:
    p = PROFILES[profile]
    rng = random.Random(p["seed"])
    out.mkdir(parents=True, exist_ok=True)
    weeks = weeks_for(p["anchor_saturday"], p["weeks"])
    orgs, zips = organizations(p, rng)
    sale_rows = sales(p, orgs, rng, weeks)
    products = [dict(zip(PRODUCT_FIELDS, (x[0], x[1], x[2], x[3], "Injectable", x[4], "Oncology",
                                          x[5], x[6], x[7], x[8]))) for x in p["products"]]
    for name, fields, rows in (("organizations.csv", ORG_FIELDS, orgs),
                               ("products.csv", PRODUCT_FIELDS, products),
                               ("zip_territory.csv", ZIP_FIELDS, zips),
                               ("sales.csv", SALES_FIELDS, sale_rows)):
        with (out / name).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    (out / "users.json").write_text(json.dumps(users(p, sale_rows, orgs, zips), indent=1))
    docs, expected = batches(profile, p, orgs, weeks)
    (out / "batches").mkdir(exist_ok=True)
    for i, d in enumerate(docs, 1):
        (out / "batches" / f"{i:02d}-{d['batch_id']}.json").write_text(json.dumps(d, indent=1))

    sys.path.insert(0, str(ROOT))
    from app.data.classification import RULE_VERSION
    from app.data.manifest import MAPPING_VERSION
    from app.data.schema_contract import CONTRACT_VERSION
    files = sorted(x for x in out.rglob("*") if x.is_file() and x.name != "manifest.json")
    manifest = {
        "profile": profile, "description": p["description"], "seed": p["seed"],
        "generator": "scripts/fixture_profile.py", "generator_version": GENERATOR_VERSION,
        "contracts": {"schema_contract": CONTRACT_VERSION, "mapping": MAPPING_VERSION,
                      "classification_rule": RULE_VERSION},
        "anchor_saturday": p["anchor_saturday"], "weeks": p["weeks"],
        "rows": {"organizations": len(orgs), "products": len(products), "zip_territory": len(zips),
                 "sales": len(sale_rows)},
        "true_classification": {x[0]: x[10] for x in p["products"]},
        "market_gap_weeks": p["market_gap_weeks"], "silent_product": p["silent_product"],
        "expected_ingestion": expected,
        "files": {str(x.relative_to(out)): hashlib.sha256(x.read_bytes()).hexdigest() for x in files},
        "real_data": False,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", choices=sorted(PROFILES), required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    manifest = write(args.out, args.profile)
    print(json.dumps({k: manifest[k] for k in ("profile", "seed", "rows", "contracts")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
