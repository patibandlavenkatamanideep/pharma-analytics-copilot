"""Incremental ingestion, end to end, against a disposable database.

Every test starts from a fresh seed load of pharma_analytics_ingesttest
(scripts/build_ingesttest_db.py) and applies real batches through
app.data.ingest.ingest -- the same function scripts/ingest.py runs. Expected
values are computed here from the events themselves, never read back from
the code under test.

The seed calendar ends on Sunday 2026-09-20 and assigns a week to the month
holding most of its days; its week offsets are not distances in weeks (08-30
is offset 4, not 3), which several tests rely on.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.data.sources import SourceBatch, SourceEvent

INGEST_DB = os.environ.get("PAC_INGESTTEST_DB", "pharma_analytics_ingesttest")
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
SOURCE = "test-feed"
IN_TERRITORY = "FA001"       # New York Metro
DISTRIBUTOR_NDC = "11111-0101-01"


@pytest.fixture(scope="module")
def ingest_env():
    import psycopg

    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = INGEST_DB
    get_settings.cache_clear()
    close_pools()
    clear_caches()
    try:
        try:
            psycopg.connect(get_settings().dsn("owner"), connect_timeout=3).close()
        except psycopg.OperationalError:
            pytest.skip(f"{INGEST_DB} is absent; run scripts/build_ingesttest_db.py")
        yield
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


@pytest.fixture
def fresh(ingest_env):
    """A fresh seed publication, with no batch history."""
    from app.data.loader import load
    from app.db import owner_transaction

    load("seed")
    with owner_transaction() as cur:
        cur.execute("DELETE FROM app_ingest.quarantine")
        cur.execute("DELETE FROM app_ingest.batches")
    return state()


# -- helpers -----------------------------------------------------------------

def q(sql: str, params: tuple = ()):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def state() -> dict:
    row = q("SELECT count(*) AS n, coalesce(sum(pack_units::numeric), 0) AS packs FROM sales")[0]
    gen = q("SELECT dataset_id FROM app_ref.generation")[0]["dataset_id"]
    return {"rows": row["n"], "packs": row["packs"], "generation": gen}


def at(local: str) -> datetime:
    """A New York wall-clock time, as an aware datetime."""
    return datetime.fromisoformat(local).replace(tzinfo=NY)


def ev(event_id: str, when: datetime | str = "2026-09-18T10:00", *, version: int = 1,
       packs: float = 10, **kw) -> SourceEvent:
    if isinstance(when, str):
        when = at(when)
    base = dict(source_event_id=event_id, event_version=version, kind="upsert",
                event_time=when, org_id=IN_TERRITORY, ndc=DISTRIBUTOR_NDC,
                data_source="distributor", pack_units=packs, unit="packs", wac=1200.0)
    base.update(kw)
    return SourceEvent(**base)


def delete(event_id: str, version: int) -> SourceEvent:
    return SourceEvent(source_event_id=event_id, event_version=version, kind="delete",
                       event_time=at("2026-09-25T09:00"))


def batch(batch_id: str, *events: SourceEvent, **overrides) -> SourceBatch:
    return SourceBatch.of(SOURCE, batch_id, events, **overrides)


def run(b: SourceBatch):
    from app.data.ingest import ingest
    return ingest(b, now=NOW)


def filler(prefix: str, n: int) -> list[SourceEvent]:
    return [ev(f"{prefix}-{i}", packs=1) for i in range(n)]


def sale_for(event_id: str):
    rows = q("SELECT s.* FROM sales s JOIN app_ingest.event_ledger l USING (sale_id) "
             "WHERE l.source_system = %s AND l.source_event_id = %s", (SOURCE, event_id))
    return rows[0] if rows else None


# -- publication ---------------------------------------------------------------

def test_new_events_publish_a_new_generation_with_derived_columns(fresh):
    out = run(batch("b1", ev("e1", packs=7), ev("e2", packs=5, ndc="11111-0201-01")))

    assert out.status == "published" and out.applied == 2
    after = state()
    assert after["rows"] == fresh["rows"] + 2
    assert after["packs"] == fresh["packs"] + 12
    assert after["generation"] == out.dataset_id != fresh["generation"]
    manifest = q("SELECT load_mode, load_state, parent_dataset_id, ingest_batch_id "
                 "FROM app_meta.dataset_manifest WHERE dataset_id = %s", (out.dataset_id,))[0]
    assert manifest == {"load_mode": "seed", "load_state": "published",
                        "parent_dataset_id": fresh["generation"],
                        "ingest_batch_id": f"{SOURCE}/b1"}
    assert q("SELECT count(*) AS n FROM app_meta.dataset_manifest "
             "WHERE load_state = 'published'")[0]["n"] == 1

    row = sale_for("e2")
    product = q("SELECT * FROM products WHERE ndc = '11111-0201-01'")[0]
    org = q("SELECT * FROM organizations WHERE org_id = %s", (IN_TERRITORY,))[0]
    assert (row["drug_name"], row["brand_flag"], row["specialty"]) == (
        product["drug_name"], product["brand_flag"], product["specialty"])
    assert row["state"] == org["state"]
    assert row["total_mg"] == pytest.approx(5 * product["mg_equivalent"])
    # Friday 18 Sep is in the week ending Sunday 20 Sep -- an existing week,
    # so it takes that week's published labels and offsets.
    assert (row["transaction_date"], row["week_ending_date"], row["period_wk"],
            row["period_mo"], row["wk_offset"], row["mo_offset"]) == (
        "2026-09-18", "2026-09-20", "2026-W38", "2026-09", 0, 0)


def test_replaying_a_batch_changes_nothing_and_publishes_nothing(fresh):
    b = batch("b1", ev("e1", packs=7), ev("e2", packs=7))
    first = run(b)
    after_first = state()
    again = run(b)

    assert again.status == "no_change" and again.duplicates == 2 and again.applied == 0
    assert state() == after_first
    row = q("SELECT status, attempts, dataset_id FROM app_ingest.batches "
            "WHERE batch_id = 'b1'")[0]
    assert row == {"status": "no_change", "attempts": 2, "dataset_id": first.dataset_id}


def test_a_correction_applied_twice_updates_the_fact_once(fresh):
    run(batch("b1", ev("e1", packs=10)))
    correction = batch("b2", ev("e1", version=2, packs=14))
    first = run(correction)
    second = run(correction)

    assert (first.status, first.corrected, first.pack_delta) == ("published", 1, Decimal(4))
    assert (second.status, second.duplicates) == ("no_change", 1)
    assert state()["rows"] == fresh["rows"] + 1
    assert state()["packs"] == fresh["packs"] + 14
    assert sale_for("e1")["pack_units"] == 14


def test_an_older_version_arriving_late_does_not_undo_a_correction(fresh):
    run(batch("b1", ev("e1", version=2, packs=14)))
    out = run(batch("b2", ev("e1", version=1, packs=10)))
    assert (out.status, out.duplicates) == ("no_change", 1)
    assert sale_for("e1")["pack_units"] == 14


def test_a_tombstone_removes_once_and_a_late_copy_cannot_resurrect(fresh):
    original = ev("e1", packs=9)
    run(batch("b1", original))
    gone = run(batch("b2", delete("e1", 2)))
    late_copy = run(batch("b3", original))

    assert (gone.status, gone.tombstoned, gone.pack_delta) == ("published", 1, Decimal(-9))
    assert (late_copy.status, late_copy.duplicates) == ("no_change", 1)
    assert state()["rows"] == fresh["rows"]
    assert state()["packs"] == fresh["packs"]
    ledger = q("SELECT tombstoned, sale_id FROM app_ingest.event_ledger "
               "WHERE source_event_id = 'e1'")[0]
    assert ledger == {"tombstoned": True, "sale_id": None}


def test_a_deletion_that_arrives_before_its_sale_still_wins(fresh):
    early_delete = run(batch("b1", delete("e1", 2)))
    the_sale = run(batch("b2", ev("e1", version=1)))
    assert (early_delete.status, early_delete.tombstoned) == ("no_change", 1)
    assert (the_sale.status, the_sale.duplicates) == ("no_change", 1)
    assert state()["rows"] == fresh["rows"]


def test_a_newer_version_after_a_deletion_reissues_the_sale(fresh):
    run(batch("b1", ev("e1", packs=3)))
    run(batch("b2", delete("e1", 2)))
    out = run(batch("b3", ev("e1", version=3, packs=4)))
    assert (out.status, out.applied) == ("published", 1)
    assert sale_for("e1")["pack_units"] == 4


def test_two_sales_with_identical_amounts_are_two_sales(fresh):
    same = dict(when="2026-09-17T09:00", packs=12)
    out = run(batch("b1", ev("e1", **same), ev("e2", **same)))
    assert out.applied == 2 and out.duplicates == 0
    assert state()["packs"] == fresh["packs"] + 24


def test_one_identity_and_version_with_two_contents_is_quarantined(fresh):
    contradictory = [ev("e1", packs=10), ev("e1", packs=11)]
    out = run(batch("b1", *contradictory, *filler("ok", 40)))
    assert out.status == "published"
    assert out.quarantine_reasons == {"conflicting_versions": 2}
    assert sale_for("e1") is None

    run(batch("b2", ev("e2", packs=10), *filler("ok2", 40)))
    clash = run(batch("b3", ev("e2", packs=99), *filler("ok3", 40)))
    assert clash.quarantine_reasons == {"conflicting_versions": 1}
    assert sale_for("e2")["pack_units"] == 10


# -- reconciliation and quality --------------------------------------------------

def test_a_batch_that_does_not_reconcile_is_rejected_whole(fresh):
    short = batch("b1", ev("e1", packs=5), ev("e2", packs=5), declared_count=3)
    off = batch("b2", ev("e3", packs=5), declared_pack_units=Decimal("6"))
    for b in (short, off):
        out = run(b)
        assert out.status == "rejected"
        assert "control totals do not reconcile" in out.rejection_reason
    assert state() == fresh
    assert {r["batch_id"]: r["status"] for r in q("SELECT batch_id, status FROM "
                                                  "app_ingest.batches")} == {
        "b1": "rejected", "b2": "rejected"}


def test_a_broken_feed_is_rejected_and_a_noisy_one_is_published(fresh):
    broken = run(batch("b1", ev("bad", org_id="NOPE"), *filler("ok", 9)))     # 10%
    assert broken.status == "rejected" and broken.quarantined == 1
    assert state() == fresh

    noisy = run(batch("b2", ev("bad", org_id="NOPE"), *filler("ok", 39)))     # 2.5%
    assert (noisy.status, noisy.applied, noisy.quarantined) == ("published", 39, 1)
    held = q("SELECT source_event_id, reason, attempt FROM app_ingest.quarantine "
             "WHERE batch_id = 'b2'")
    assert held == [{"source_event_id": "bad", "reason": "unknown_organization", "attempt": 1}]


@pytest.mark.parametrize(("change", "reason"), [
    ({"org_id": "NOPE"}, "unknown_organization"),
    ({"ndc": "00000-0000-00"}, "unknown_product"),
    ({"unit": "mg"}, "unit_not_packs"),
    ({"data_source": "pharmacy"}, "unknown_data_source"),
    ({"pack_units": 0}, "non_positive_packs"),
    ({"wac": 0.0}, "invalid_price"),
    ({"wac": -1.0}, "invalid_price"),
    ({"data_source": "hub_dispense", "wac": 50.0}, "priced_free_drug"),
    ({"event_time": datetime(2026, 9, 18, 10)}, "naive_timestamp"),
    ({"event_time": NOW + timedelta(days=2)}, "future_event"),
    ({"event_time": at("2026-01-15T10:00")}, "before_history"),
    ({"org_id": None}, "missing_field"),
])
def test_each_invalid_event_is_quarantined_with_its_reason(fresh, change, reason):
    bad = replace(ev("bad"), **change)
    out = run(batch("b1", bad, *filler("ok", 39)))
    assert out.status == "published"
    assert out.quarantine_reasons == {reason: 1}
    assert sale_for("bad") is None


# -- the calendar ----------------------------------------------------------------

def test_the_business_timezone_decides_the_day(fresh):
    # 02:30 UTC on Monday 21 Sep is still Sunday evening in New York.
    sunday_night = datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc)
    out = run(batch("b1", ev("e1", sunday_night)))
    row = sale_for("e1")
    assert (row["transaction_date"], row["week_ending_date"]) == ("2026-09-20", "2026-09-20")
    assert out.anchor_shift_weeks == 0


def test_a_new_week_advances_the_anchor_and_every_offset(fresh):
    before = {r["week_ending_date"]: r["wk_offset"] for r in q("SELECT * FROM app_ref.calendar")}
    out = run(batch("b1", ev("e1", "2026-09-24T10:00")))     # week ending Sun 27 Sep

    assert (out.status, out.anchor_shift_weeks, out.anchor_shift_months) == ("published", 1, 0)
    after = {r["week_ending_date"]: r for r in q("SELECT * FROM app_ref.calendar")}
    assert after["2026-09-27"]["wk_offset"] == 0
    assert after["2026-09-27"]["period_wk"] == "2026-W39"
    assert {we: after[we]["wk_offset"] for we in before} == {we: o + 1 for we, o in before.items()}
    # Every fact agrees with the calendar it is published with.
    assert q("SELECT count(*) AS n FROM sales s JOIN app_ref.calendar c "
             "ON c.week_ending_date = s.week_ending_date "
             "WHERE (c.wk_offset, c.mo_offset) <> (s.wk_offset, s.mo_offset)")[0]["n"] == 0
    anchor = q("SELECT reporting_anchor FROM app_meta.dataset_manifest "
               "WHERE load_state = 'published'")[0]["reporting_anchor"]
    assert anchor["max_week_ending"] == "2026-09-27"


def test_a_new_month_moves_month_offsets_and_relative_periods(fresh, ingest_env):
    from app.analytics.compiler import Compiler
    from app.analytics.plan import AnalyticalPlan
    from app.db import analytics_transaction

    # Tue 29 Sep is in the week ending Sun 4 Oct, most of which is October.
    out = run(batch("b1", ev("e1", "2026-09-29T10:00", packs=6)))
    assert (out.anchor_shift_weeks, out.anchor_shift_months) == (2, 1)
    assert sale_for("e1")["period_mo"] == "2026-10"
    manifest = q("SELECT reporting_anchor, warnings FROM app_meta.dataset_manifest "
                 "WHERE load_state = 'published'")[0]
    assert manifest["reporting_anchor"]["max_period_mo"] == "2026-10"
    # The week ending 27 Sep has no rows in any source: said, not hidden.
    assert "calendar_gaps" in {w["code"] for w in manifest["warnings"]}

    plan = AnalyticalPlan.model_validate({
        "metric": "paid_pack_units", "time": {"kind": "named", "named": "current_month"}})
    query = Compiler().compile(plan, anchor=manifest["reporting_anchor"])
    with analytics_transaction(scope_kind="global", scope_value=None,
                               wac_authorized=True) as cur:
        cur.execute(query.sql, query.params)
        assert float(cur.fetchone()["value"]) == 6.0


def test_a_late_event_in_a_published_week_is_counted_and_its_period_reported(fresh):
    out = run(batch("b1", ev("e1", "2026-08-27T10:00")))     # week ending 30 Aug
    assert (out.status, out.late, out.anchor_shift_weeks) == ("published", 1, 0)
    assert out.affected_periods == ["2026-08"]
    assert sale_for("e1")["wk_offset"] == 4


def test_a_missing_week_with_room_in_the_calendar_is_placed(fresh):
    out = run(batch("b1", ev("e1", "2026-09-03T10:00")))     # week ending 6 Sep
    assert out.status == "published"
    week = q("SELECT * FROM app_ref.calendar WHERE week_ending_date = '2026-09-06'")[0]
    assert (week["wk_offset"], week["period_wk"], week["period_mo"]) == (2, "2026-W36", "2026-09")


def test_a_missing_week_the_calendar_has_no_room_for_is_held_back(fresh):
    # Week ending 23 Aug is three weeks before 13 Sep (offset 1) -- offset 4,
    # which the seed already gave to 30 Aug. Giving it any label would
    # reorder or duplicate a published week.
    out = run(batch("b1", ev("e1", "2026-08-20T10:00"), *filler("ok", 39)))
    assert out.quarantine_reasons == {"calendar_conflict": 1}


# -- concurrency, failure, reload -------------------------------------------------

def test_a_request_planned_on_the_old_generation_is_told_to_refresh(fresh):
    from app.db import GenerationChanged, analytics_transaction

    run(batch("b1", ev("e1")))
    with pytest.raises(GenerationChanged):
        with analytics_transaction(scope_kind="global", scope_value=None, wac_authorized=True,
                                   expect_generation=fresh["generation"]) as cur:
            cur.execute("SELECT 1")


def test_ingestion_waits_for_another_publication(fresh):
    import psycopg

    from app.config import get_settings

    holder = psycopg.connect(get_settings().dsn("owner"))
    holder.execute("SELECT pg_advisory_xact_lock(hashtext('pac:publication'))")
    done = threading.Event()
    result = {}

    def worker():
        result["out"] = run(batch("b1", ev("e1")))
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert not done.wait(1.0), "ingestion published while another publication held the lock"
    finally:
        holder.rollback()
        holder.close()
    t.join(30)
    assert result["out"].status == "published"


def test_a_failure_after_writing_rolls_back_everything_and_is_recorded(fresh, monkeypatch):
    import app.data.ingest as ingest_module

    def boom(cur, report=None):
        raise RuntimeError("calendar rebuild failed")

    monkeypatch.setattr(ingest_module, "_populate_calendar", boom)
    with pytest.raises(RuntimeError):
        run(batch("b1", ev("e1"), ev("e2")))
    assert state() == fresh
    assert q("SELECT count(*) AS n FROM app_ingest.event_ledger")[0]["n"] == 0
    row = q("SELECT status, applied, rejection_reason FROM app_ingest.batches")[0]
    assert row["status"] == "rejected" and row["applied"] == 0
    assert "calendar rebuild failed" in row["rejection_reason"]


def test_a_full_reload_forgets_the_ledger_so_events_apply_to_the_new_base(fresh):
    from app.data.loader import load

    b = batch("b1", ev("e1", packs=8))
    run(b)
    load("seed")
    assert q("SELECT count(*) AS n FROM app_ingest.event_ledger")[0]["n"] == 0
    out = run(b)
    assert (out.status, out.applied) == ("published", 1)


def test_a_scoped_user_sees_ingested_sales_only_inside_their_scope(fresh):
    from app.auth.policy import principal_for_user_id
    from app.db import analytics_transaction

    ram = principal_for_user_id("U009")          # New York Metro
    outside = q("SELECT o.org_id FROM organizations o JOIN zip_territory z USING (zip) "
                "WHERE z.territory_name <> 'New York Metro' AND o.org_status = 'Active' "
                "ORDER BY 1 LIMIT 1")[0]["org_id"]

    def visible():
        with analytics_transaction(scope_kind=ram.scope_kind, scope_value=ram.scope_value,
                                   wac_authorized=False) as cur:
            cur.execute("SELECT coalesce(sum(pack_units), 0) AS v FROM sales")
            return float(cur.fetchone()["v"])

    before = visible()
    run(batch("b1", ev("in", packs=5), ev("out", packs=50, org_id=outside)))
    assert visible() == before + 5


def test_the_synthetic_feed_replays_identically(ingest_env):
    from app.data.sources import SyntheticIncrementalSource

    def feed():
        return [b for b in SyntheticIncrementalSource(
            orgs=["A", "B"], ndcs=["N"], latest_week_ending=NOW, seed=3).batches()]

    assert feed() == feed()


def test_a_two_hundred_event_batch_does_not_scan_per_event(fresh):
    start = time.perf_counter()
    run(batch("b1", *filler("t", 200)))
    elapsed = time.perf_counter() - start
    # A bound loose enough for CI, tight enough to catch a per-event table scan.
    assert elapsed < 30, elapsed


def test_users_are_told_how_fresh_the_data_is(fresh, monkeypatch):
    import app.api.main as api
    from app.auth.policy import principal_for_user_id
    from app.llm.planner import OfflinePlanner
    from app.pipeline import Pipeline

    monkeypatch.setattr(api, "_pipeline", Pipeline(OfflinePlanner()))
    me = api.me

    exec_user = principal_for_user_id("U001")
    before = me(exec_user)["dataset"]
    assert (before["incremental"], before["last_ingest_at"]) == (False, None)
    assert before["data_through"] == "2026-09-15"

    run(batch("b1", ev("e1", "2026-09-24T10:00")))
    after = me(exec_user)["dataset"]
    assert after["data_through"] == "2026-09-24"
    assert after["incremental"] is True and after["last_ingest_at"] is not None
    assert after["published_at"] > before["published_at"]


@pytest.mark.parametrize("settle, reclaimed", [(0, False), (2.5, True)],
                         ids=["immediate", "after-settling"])
def test_rows_replaced_by_a_publication_are_reclaimed_once_readers_finish(
        fresh, monkeypatch, settle, reclaimed):
    """A reader that started before the commit can still see the replaced
    rows, so a VACUUM run immediately reclaims none of them. Waiting out the
    reader first reclaims them all."""
    from app.config import get_settings
    from app.db import analytics_transaction

    run(batch("b1", *filler("r", 20)))                    # rows to correct
    monkeypatch.setattr(get_settings(), "publication_settle_seconds", settle)
    holding, released = threading.Event(), threading.Event()

    def reader():
        with analytics_transaction(scope_kind="global", scope_value=None,
                                   wac_authorized=False) as cur:
            cur.execute("SELECT count(*) FROM sales")
            holding.set()
            released.wait(10)

    t = threading.Thread(target=reader)
    t.start()
    holding.wait(10)
    threading.Timer(1.0, released.set).start()
    corrections = [ev(f"r-{i}", version=2, packs=2) for i in range(20)]
    out = run(batch("b2", *corrections))
    t.join(10)
    assert out.status == "published" and out.corrected == 20
    left = out.dead_rows_after_reclaim["sales"]
    assert (left == 0) is reclaimed, left


def test_an_anchor_shift_keeps_every_sale_id_and_the_period_order(fresh):
    """The shift rewrites every row. It must not renumber them -- the ledger
    points at sale_ids -- and it must leave the table stored in period
    order, which an UPDATE does not (see ingest._shift_offsets)."""
    run(batch("b1", *filler("k", 5)))
    before = {r["sale_id"] for r in q("SELECT sale_id FROM sales")}
    ledger = {r["sale_id"] for r in q("SELECT sale_id FROM app_ingest.event_ledger "
                                      "WHERE sale_id IS NOT NULL")}
    out = run(batch("b2", ev("new", "2026-09-24T10:00")))
    assert out.anchor_shift_weeks == 1
    after = {r["sale_id"] for r in q("SELECT sale_id FROM sales")}
    assert before < after and ledger <= after
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("ANALYZE sales")
    correlation = q("SELECT correlation FROM pg_stats WHERE tablename = 'sales' "
                    "AND attname = 'mo_offset'")[0]["correlation"]
    assert correlation > 0.9, correlation
