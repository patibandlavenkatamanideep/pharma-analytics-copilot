"""A calendar that loads but cannot be extended is said so at load.

schema/generate_data.py labels a week with its Saturday's CALENDAR year and
its ISO week number. Across a year end that falls in ISO week 53 the label is
wrong (Saturday 2021-01-02 is 2020-W53, labelled 2021-W53). The bulk load
accepts any labels; incremental ingestion checks the convention before it
adds a week, and refuses. Found while building the fixture profiles
(qualification of 7 October 2026, step 3): a dataset in that state looked
ready and could take no new data, and only the first batch said why.

The orchard profile's history crosses that year end. Relabelled the way the
supplied generator labels weeks, it is loaded into a disposable database of
its own (never the working or release database).
"""

from __future__ import annotations

import csv
import json
import os
import pathlib
import subprocess
import sys

import pytest

from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]
ROOT = pathlib.Path(__file__).resolve().parents[2]
DB = f"{os.environ.get('PAC_PROFILE_DB_PREFIX', 'pac_profile_')}weeklabels"


def supplied_label(week_ending: str) -> str:
    """schema/generate_data.py: the Saturday's calendar year, its ISO week."""
    from datetime import date
    day = date.fromisoformat(week_ending)
    return f"{day.year}-W{day.isocalendar()[1]:02d}"


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools, owner_transaction

    assert "profile" in DB and DB != os.environ.get("PAC_DB_NAME"), DB
    data = tmp_path_factory.mktemp("weeklabels")
    env = {**os.environ, "PAC_DB_NAME": DB, "PYTHONPATH": str(ROOT),
           "PAC_PUBLICATION_SETTLE_SECONDS": "0", "PAC_LLM_PROVIDER": "offline"}
    for argv in ([sys.executable, str(ROOT / "scripts" / "fixture_profile.py"),
                  "--profile", "orchard", "--out", str(data)],
                 [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py"), "--drop", "--no-env"]):
        r = subprocess.run(argv, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-2500:]
    with (data / "sales.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    relabelled = [r for r in rows if r["period_wk"] != supplied_label(r["week_ending_date"])]
    assert relabelled, "the profile's history crosses an ISO week-53 year end"
    for r in rows:
        r["period_wk"] = supplied_label(r["week_ending_date"])
    with (data / "sales.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = DB
    get_settings.cache_clear()
    close_pools()
    clear_caches()
    try:
        from app.data.loader import load
        with owner_transaction() as cur:
            for u in json.loads((data / "users.json").read_text()):
                cur.execute("INSERT INTO users (user_id, email, full_name, role, territory_name, "
                            "region_name, can_view_wac) VALUES (%(user_id)s, %(email)s, "
                            "%(full_name)s, %(role)s, %(territory_name)s, %(region_name)s, "
                            "%(can_view_wac)s)", u)
        report = load("full", generated_dir=data)
        yield {"report": report, "dir": data}
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


@pytest.mark.xfail(strict=True, reason=(
    "the bulk load accepts week labels incremental ingestion cannot extend, and says "
    "nothing; the first batch is refused"))
def test_the_load_says_the_calendar_cannot_be_extended_and_why(loaded):
    report = loaded["report"]
    codes = {w["code"]: w for w in report.warnings}
    assert "calendar_not_extendable" in codes, sorted(codes)
    assert "ISO week" in codes["calendar_not_extendable"]["message"]
    calendar = report.source_coverage["calendar"]
    assert calendar["extendable"] is False and "2021-W53" in calendar["reason"], calendar


def test_ingestion_then_refuses_for_the_reason_the_load_gave(loaded):
    from app.data.ingest import ingest
    from app.data.sources import JsonBatchFiles

    first = sorted((loaded["dir"] / "batches").glob("*.json"))[0]
    [batch] = list(JsonBatchFiles([first]).batches())
    outcome = ingest(batch).as_dict()
    assert outcome["status"] == "rejected" and outcome["rejection_code"] == "calendar_unextendable", outcome
