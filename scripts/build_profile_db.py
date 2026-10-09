#!/usr/bin/env python3
"""Build a disposable database from a fixture profile and reconcile its onboarding.

    python3 scripts/build_profile_db.py --profile orchard [--db pac_profile_orchard]
                                        [--dir <generated>] [--report <report.json>]

Generates the profile (scripts/fixture_profile.py), bootstraps a disposable
database, inserts the profile's own users (so scoped roles have nonempty
territories in its geography), loads it through the ordinary full load,
ingests its batches through the ordinary reader and ingestion, and writes a
machine-readable onboarding report: every batch's outcome, and every event's
expected outcome (from the profile's manifest) against what the ledger,
quarantine, batch log and sales tables actually hold. Every received record
must reconcile to exactly one of accepted, quarantined, ignored replay or
rejected, and the published total must move by exactly the applied changes.

Refuses any database whose name does not contain "profile", so it cannot
touch the working or release databases.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def build(profile: str, db: str, directory: pathlib.Path, report_path: pathlib.Path) -> dict:
    if "profile" not in db or db == "pharma_analytics":
        raise SystemExit(f"refusing database {db!r}: a profile database name contains 'profile'")
    env = {**os.environ, "PAC_DB_NAME": db, "PYTHONPATH": str(ROOT),
           "PAC_PUBLICATION_SETTLE_SECONDS": "0", "PAC_LLM_PROVIDER": "offline"}
    steps = [([sys.executable, str(ROOT / "scripts" / "fixture_profile.py"), "--profile", profile,
               "--out", str(directory)], "generate"),
             ([sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"), "--drop", "--no-env"],
              "bootstrap"),
             ([sys.executable, __file__, "--populate", "--profile", profile, "--db", db,
               "--dir", str(directory), "--report", str(report_path)], "populate")]
    for argv, label in steps:
        r = subprocess.run(argv, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{label} failed: {r.stdout[-1500:]} {r.stderr[-2500:]}")
    return json.loads(report_path.read_text())


def populate(profile: str, directory: pathlib.Path, report_path: pathlib.Path) -> int:
    from decimal import Decimal

    from app.data.ingest import ingest
    from app.data.loader import load
    from app.data.sources import JsonBatchFiles
    from app.db import close_pools, owner_transaction

    manifest = json.loads((directory / "manifest.json").read_text())
    with owner_transaction() as cur:
        for u in json.loads((directory / "users.json").read_text()):
            cur.execute("INSERT INTO users (user_id, email, full_name, role, territory_name, "
                        "region_name, can_view_wac) VALUES (%(user_id)s, %(email)s, %(full_name)s, "
                        "%(role)s, %(territory_name)s, %(region_name)s, %(can_view_wac)s)", u)
    loaded = load("full", generated_dir=directory)

    def total() -> Decimal:
        with owner_transaction() as cur:
            cur.execute("SELECT coalesce(sum(pack_units::numeric), 0) AS t FROM sales")
            return cur.fetchone()["t"]

    base_total = total()
    outcomes = []
    for path in sorted((directory / "batches").glob("*.json")):
        for batch in JsonBatchFiles([path]).batches():
            outcomes.append(ingest(batch).as_dict())
    final_total = total()

    source = f"{profile}-feed"
    with owner_transaction() as cur:
        cur.execute("SELECT l.source_event_id, l.event_version, l.tombstoned, l.sale_id, "
                    "s.pack_units FROM app_ingest.event_ledger l LEFT JOIN sales s "
                    "ON s.sale_id = l.sale_id WHERE l.source_system = %s", (source,))
        ledger = {r["source_event_id"]: r for r in cur.fetchall()}
        cur.execute("SELECT batch_id, source_event_id, reason FROM app_ingest.quarantine "
                    "WHERE source_system = %s", (source,))
        quarantine = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT batch_id, status, rejection_code, received_count FROM app_ingest.batches "
                    "WHERE source_system = %s", (source,))
        batch_log = {r["batch_id"]: dict(r) for r in cur.fetchall()}
        cur.execute("SELECT max(week_ending_date) AS w FROM app_ref.calendar")
        anchor_after = str(cur.fetchone()["w"])

    events = []
    final_state: dict[str, dict] = {}
    for e in manifest["expected_ingestion"]:
        final_state.setdefault(e["source_event_id"], {})
        final_state[e["source_event_id"]] = e   # the last expectation for an id is its final one
    for e in manifest["expected_ingestion"]:
        sid, kind = e["source_event_id"], e["outcome"]
        batch_id = f"{profile}-{e['batch_id']}"
        row = ledger.get(sid)
        if kind.startswith("quarantined:"):
            reason = kind.split(":", 1)[1]
            ok = any(q["batch_id"] == batch_id and q["reason"] == reason
                     and (q["source_event_id"] or "") == sid for q in quarantine)
        elif kind.startswith("rejected:"):
            code = kind.split(":", 1)[1]
            ok = (batch_log.get(batch_id, {}).get("rejection_code") == code and row is None)
        elif kind in ("insert", "correction"):
            last = final_state[sid]
            if last is not e:     # superseded later in the series; checked at its final step
                ok = row is not None
            else:
                ok = (row is not None and not row["tombstoned"] and row["event_version"] == e["event_version"])
        elif kind in ("tombstone", "tombstone_without_sale"):
            ok = row is not None and row["tombstoned"] and row["sale_id"] is None
        elif kind == "duplicate":
            ok = row is not None    # no change: the ledger keeps its own (newer or equal) version
        else:
            ok = False
        events.append({**e, "observed": None if row is None else
                       {"event_version": row["event_version"], "tombstoned": row["tombstoned"],
                        "pack_units": None if row["pack_units"] is None else float(row["pack_units"])},
                       "reconciled": ok})

    applied_delta = sum(Decimal(str(e["packs_delta"])) for e in manifest["expected_ingestion"])
    categories = {"accepted": 0, "quarantined": 0, "ignored_replay": 0, "rejected": 0}
    for e in manifest["expected_ingestion"]:
        o = e["outcome"]
        key = ("quarantined" if o.startswith("quarantined") else "rejected" if o.startswith("rejected")
               else "ignored_replay" if o == "duplicate" else "accepted")
        categories[key] += 1
    received = sum(b["received_count"] for b in batch_log.values())
    report = {
        "profile": profile, "seed": manifest["seed"], "contracts": manifest["contracts"],
        "real_data": False,
        "load": {"dataset_id": loaded.dataset_id, "row_counts": loaded.row_counts,
                 "warnings": [w["code"] if isinstance(w, dict) else str(w) for w in loaded.warnings]},
        "batches": outcomes,
        "batch_log": batch_log,
        "events": events,
        "reconciliation": {
            "events_expected": len(manifest["expected_ingestion"]),
            "events_received_by_batches": received,
            "by_category": categories,
            "every_event_reconciled": all(e["reconciled"] for e in events),
            "categories_cover_every_event": sum(categories.values()) == len(manifest["expected_ingestion"]),
            "base_total_packs": str(base_total), "final_total_packs": str(final_total),
            "expected_delta": str(applied_delta), "observed_delta": str(final_total - base_total),
            "totals_reconcile": final_total - base_total == applied_delta,
            "anchor_before": manifest["anchor_saturday"], "anchor_after": anchor_after,
        },
    }
    report_path.write_text(json.dumps(report, indent=1, default=str))
    close_pools()
    print(json.dumps(report["reconciliation"], default=str))
    return 0 if report["reconciliation"]["every_event_reconciled"] and \
        report["reconciliation"]["totals_reconcile"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--dir", type=pathlib.Path, default=None)
    ap.add_argument("--report", type=pathlib.Path, default=None)
    ap.add_argument("--populate", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    db = args.db or f"pac_profile_{args.profile}"
    directory = args.dir or pathlib.Path(tempfile.mkdtemp(prefix=f"profile-{args.profile}-"))
    report = args.report or directory / "onboarding-report.json"
    if args.populate:
        return populate(args.profile, directory, report)
    result = build(args.profile, db, directory, report)
    print(json.dumps(result["reconciliation"], default=str))
    return 0 if result["reconciliation"]["every_event_reconciled"] and \
        result["reconciliation"]["totals_reconcile"] else 1


if __name__ == "__main__":
    sys.exit(main())
