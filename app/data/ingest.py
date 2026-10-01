"""Incremental ingestion: apply a source batch to the published dataset and
publish the result as a new generation.

A batch goes through, in one owner transaction:

1. **Reconcile** what arrived against the source's declared control totals.
   A mismatch means the batch is incomplete or corrupt; nothing is applied.
2. **Validate** each event -- known organization and product, a stated unit
   of packs, a timezone-aware timestamp, a price consistent with its source
   -- and **place** it in the reporting calendar. Failures are quarantined
   with a reason, not dropped.
3. **Classify** each event against the ledger by (source_system,
   source_event_id): new, a correction (higher version), a duplicate (same
   version, same content -- or an older version), a conflict (same version,
   different content), or a deletion. Identity, never amount, decides: two
   sales of 10 packs at the same account in the same week are two sales.
4. Refuse the whole batch if too many events were quarantined.
5. **Apply** inserts, corrections and tombstones; check that the change in
   total packs is exactly what the events say it should be.
6. If the latest week or month moved, **shift every offset** so that offset 0
   is again the latest period present -- the supplied facts store offsets,
   so "last month" depends on it.
7. **Rebuild the calendar** from the facts, re-run the loader's validation
   over the result, and **publish**: a new manifest row naming its parent and
   the batch, the previous one superseded, and app_ref.generation flipped --
   all in the same transaction as the facts. Readers on the old generation
   finish on the old snapshot; nothing ever sees half a batch.

A replayed batch is processed again; every event is then a duplicate, nothing
changes, and nothing is published ('no_change').

Labels for a NEW week are derived from the conventions the published calendar
already follows (week-ending weekday, how a week is assigned to a month). An
event in a week the calendar already has takes that week's published labels:
an increment never relabels history. A base whose calendar follows no
derivable convention is refused, rather than extended with guesses.

Attribution: a sales row records org_id only. Territory, region and parent
system are resolved at QUERY time from the current organizations and
zip_territory tables, so history follows the current hierarchy -- an account
moved to another territory takes its history with it, and the user who now
covers it sees that history. That is the supplied model's semantics and is
unchanged here.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.data.loader import (
    VALID_SOURCES, LoadError, _json, _populate_calendar, _validate, publication_lock,
)
from app.data.manifest import MAPPING_VERSION, LoadReport
from app.data.schema_contract import require_compatible
from app.data.sources import SourceBatch, SourceEvent
from app.db import owner_transaction

# ---------------------------------------------------------------------------
# The calendar's conventions
# ---------------------------------------------------------------------------

#: The ways a week is assigned to a reporting month that this ingests into.
#: The supplied full dataset uses the week-ending date's month; the supplied
#: seed uses the month holding most of the week's days.
MONTH_RULES = {
    "week_ending_month": lambda we: we.strftime("%Y-%m"),
    "majority_month": lambda we: (we - timedelta(days=3)).strftime("%Y-%m"),
}


def iso_week(we: date) -> str:
    year, week, _ = we.isocalendar()
    return f"{year}-W{week:02d}"


def quarter_of(period_mo: str) -> str:
    year, month = period_mo.split("-")
    return f"{year}-Q{(int(month) - 1) // 3 + 1}"


def month_index(period_mo: str) -> int:
    year, month = period_mo.split("-")
    return int(year) * 12 + int(month)


@dataclass(frozen=True)
class CalendarWeek:
    wk_offset: int
    period_wk: str
    week_ending: date
    mo_offset: int
    period_mo: str
    period_qtr: str


class ConventionError(LoadError):
    """The published calendar follows no convention new weeks could share."""


@dataclass(frozen=True)
class Convention:
    week_ending_weekday: int            # Monday = 0
    month_rules: tuple[str, ...]        # every rule the calendar agrees with

    def week_ending(self, day: date) -> date:
        return day + timedelta(days=(self.week_ending_weekday - day.weekday()) % 7)

    def labels(self, week_ending: date) -> tuple[str, str, str] | None:
        """(period_wk, period_mo, period_qtr) for a new week, or None when
        the rules the calendar is consistent with disagree about this week."""
        months = {MONTH_RULES[r](week_ending) for r in self.month_rules}
        if len(months) != 1:
            return None
        (period_mo,) = months
        return iso_week(week_ending), period_mo, quarter_of(period_mo)


def detect_convention(weeks: list[CalendarWeek]) -> Convention:
    if not weeks:
        raise ConventionError("there is no published calendar to extend")
    weekdays = {w.week_ending.weekday() for w in weeks}
    if len(weekdays) != 1:
        raise ConventionError(f"week-ending dates fall on {len(weekdays)} different weekdays")
    if bad := [w.period_wk for w in weeks if w.period_wk != iso_week(w.week_ending)]:
        raise ConventionError(f"week labels {bad[:3]} are not the ISO week of the week ending")
    if bad := [w.period_mo for w in weeks if w.period_qtr != quarter_of(w.period_mo)]:
        raise ConventionError(f"months {bad[:3]} carry a quarter label of another quarter")
    rules = tuple(name for name, rule in MONTH_RULES.items()
                  if all(rule(w.week_ending) == w.period_mo for w in weeks))
    if not rules:
        raise ConventionError("weeks are assigned to months by no recognised rule")
    months: dict[str, int] = {}
    for w in weeks:
        if months.setdefault(w.period_mo, w.mo_offset) != w.mo_offset:
            raise ConventionError(f"month {w.period_mo} carries more than one month offset")
    ordered = sorted(weeks, key=lambda w: w.week_ending)
    if any(a.wk_offset <= b.wk_offset for a, b in zip(ordered, ordered[1:])):
        raise ConventionError("week offsets do not decrease as weeks get later")
    return Convention(weekdays.pop(), rules)


@dataclass(frozen=True)
class Placement:
    transaction_date: date
    week_ending: date
    period_wk: str
    period_mo: str
    period_qtr: str
    wk_offset: int          # relative to the calendar BEFORE this batch
    mo_offset: int
    existing_week: bool


class Calendar:
    """The published calendar, extended in memory as a batch places events in
    weeks it did not have, so two events in one new week agree."""

    def __init__(self, weeks: list[CalendarWeek], convention: Convention):
        self.convention = convention
        self.by_date = {w.week_ending: w for w in weeks}
        self.dates = sorted(self.by_date)
        self.months = {w.period_mo: w.mo_offset for w in weeks}
        latest = self.by_date[self.dates[-1]]
        self.anchor = latest
        self.anchor_month = min(self.months, key=lambda m: self.months[m])

    def place(self, day: date) -> Placement | str:
        """A placement, or the quarantine reason it cannot have one."""
        we = self.convention.week_ending(day)
        if (known := self.by_date.get(we)) is not None:
            return Placement(day, we, known.period_wk, known.period_mo, known.period_qtr,
                             known.wk_offset, known.mo_offset, existing_week=True)
        if we < self.dates[0]:
            return "before_history"
        labels = self.convention.labels(we)
        if labels is None:
            return "period_convention_ambiguous"
        period_wk, period_mo, period_qtr = labels
        wk_offset = self.anchor.wk_offset + (self.anchor.week_ending - we).days // 7
        if period_mo in self.months:
            mo_offset = self.months[period_mo]
        else:
            mo_offset = self.months[self.anchor_month] + (
                month_index(self.anchor_month) - month_index(period_mo))
            if mo_offset in self.months.values():
                return "calendar_conflict"
        # The new week must sit between its neighbours: an earlier week has a
        # larger offset, a later one a smaller. A base whose offsets are not
        # distances in weeks can leave no room; the event waits for a fix
        # rather than being given a label that reorders the calendar.
        i = bisect.bisect_left(self.dates, we)
        earlier = self.by_date[self.dates[i - 1]] if i > 0 else None
        later = self.by_date[self.dates[i]] if i < len(self.dates) else None
        if (earlier and earlier.wk_offset <= wk_offset) or (later and later.wk_offset >= wk_offset):
            return "calendar_conflict"
        if any(w.wk_offset == wk_offset for w in self.by_date.values()):
            return "calendar_conflict"
        week = CalendarWeek(wk_offset, period_wk, we, mo_offset, period_mo, period_qtr)
        self.by_date[we] = week
        bisect.insort(self.dates, we)
        self.months.setdefault(period_mo, mo_offset)
        return Placement(day, we, period_wk, period_mo, period_qtr, wk_offset, mo_offset,
                         existing_week=False)


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------

@dataclass
class IngestOutcome:
    source_system: str
    batch_id: str
    status: str = "no_change"           # published | no_change | rejected
    attempt: int = 1
    dataset_id: str | None = None
    parent_dataset_id: str | None = None
    applied: int = 0
    corrected: int = 0
    tombstoned: int = 0
    duplicates: int = 0
    late: int = 0
    quarantined: int = 0
    affected_periods: list[str] = field(default_factory=list)
    anchor_shift_weeks: int = 0
    anchor_shift_months: int = 0
    rejection_reason: str | None = None
    quarantine_reasons: dict[str, int] = field(default_factory=dict)
    pack_delta: Decimal = Decimal(0)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["pack_delta"] = str(self.pack_delta)
        return d


class _Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _digest(event: SourceEvent) -> str:
    return hashlib.sha256(json.dumps(event.payload(), sort_keys=True).encode()).hexdigest()


def _decimal(value: float | None) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _field_problem(event: SourceEvent, orgs: set[str], ndcs: set[str],
                   today: date, tz: ZoneInfo) -> str | None:
    if not event.source_event_id or event.event_version is None or event.event_version < 1:
        return "invalid_identity"
    if event.kind not in ("upsert", "delete"):
        return "invalid_kind"
    if event.kind == "delete":
        return None
    if event.event_time is None:
        return "missing_field"
    if event.event_time.tzinfo is None or event.event_time.utcoffset() is None:
        return "naive_timestamp"
    if event.org_id is None or event.ndc is None or event.data_source is None \
            or event.pack_units is None:
        return "missing_field"
    if event.unit != "packs":
        return "unit_not_packs"
    if event.data_source not in VALID_SOURCES:
        return "unknown_data_source"
    if event.org_id not in orgs:
        return "unknown_organization"
    if event.ndc not in ndcs:
        return "unknown_product"
    if not event.pack_units > 0:
        return "non_positive_packs"
    if event.wac is None or event.wac < 0:
        return "invalid_price"
    if event.data_source == "distributor" and event.wac == 0:
        return "invalid_price"
    if event.data_source == "hub_dispense" and event.wac != 0:
        # Hub dispenses are free drug; a priced one is mis-sourced data.
        return "priced_free_drug"
    if event.event_time.astimezone(tz).date() > today:
        return "future_event"
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_SALES_INSERT = """
INSERT INTO sales (org_id, ndc, drug_name, data_source, brand_flag, pack_units, total_mg,
                   wac, transaction_date, week_ending_date, state, specialty,
                   period_wk, period_mo, period_qtr, wk_offset, mo_offset)
SELECT o.org_id, p.ndc, p.drug_name, %(data_source)s, p.brand_flag, %(pack_units)s,
       %(pack_units)s * p.mg_equivalent, %(wac)s, %(transaction_date)s, %(week_ending)s,
       o.state, p.specialty, %(period_wk)s, %(period_mo)s, %(period_qtr)s,
       %(wk_offset)s, %(mo_offset)s
FROM organizations o, products p
WHERE o.org_id = %(org_id)s AND p.ndc = %(ndc)s
RETURNING sale_id
"""

_SALES_UPDATE = """
UPDATE sales s SET org_id = o.org_id, ndc = p.ndc, drug_name = p.drug_name,
       data_source = %(data_source)s, brand_flag = p.brand_flag, pack_units = %(pack_units)s,
       total_mg = %(pack_units)s * p.mg_equivalent, wac = %(wac)s,
       transaction_date = %(transaction_date)s, week_ending_date = %(week_ending)s,
       state = o.state, specialty = p.specialty, period_wk = %(period_wk)s,
       period_mo = %(period_mo)s, period_qtr = %(period_qtr)s,
       wk_offset = %(wk_offset)s, mo_offset = %(mo_offset)s
FROM organizations o, products p
WHERE s.sale_id = %(sale_id)s AND o.org_id = %(org_id)s AND p.ndc = %(ndc)s
"""


def _fact_params(event: SourceEvent, placement: Placement) -> dict[str, Any]:
    return {
        "org_id": event.org_id, "ndc": event.ndc, "data_source": event.data_source,
        "pack_units": event.pack_units, "wac": event.wac,
        "transaction_date": placement.transaction_date.isoformat(),
        "week_ending": placement.week_ending.isoformat(),
        "period_wk": placement.period_wk, "period_mo": placement.period_mo,
        "period_qtr": placement.period_qtr,
        "wk_offset": placement.wk_offset, "mo_offset": placement.mo_offset,
    }


def ingest(batch: SourceBatch, *, now: datetime | None = None) -> IngestOutcome:
    """Apply one batch. Returns its outcome; a rejected batch is an outcome,
    not an exception -- it is recorded, and nothing it contained is applied."""
    settings = get_settings()
    tz = ZoneInfo(settings.business_timezone)
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    outcome = IngestOutcome(batch.source_system, batch.batch_id)
    quarantine: list[tuple[SourceEvent | None, str]] = []

    try:
        with owner_transaction() as cur:
            publication_lock(cur)
            cur.execute(
                "SELECT attempts FROM app_ingest.batches "
                "WHERE source_system = %s AND batch_id = %s FOR UPDATE",
                (batch.source_system, batch.batch_id))
            prior = cur.fetchone()
            outcome.attempt = (prior["attempts"] + 1) if prior else 1
            try:
                _process(cur, batch, outcome, quarantine, today, tz, settings)
            except _Rejected as exc:
                # Facts untouched: _process raises before its first write.
                outcome.status = "rejected"
                outcome.rejection_reason = exc.reason
            _record_batch(cur, batch, outcome, quarantine)
    except Exception as exc:
        # Something failed after writing began -- the transaction rolled back
        # whole, so none of the counts happened. Record the attempt so the
        # failure is visible, then re-raise.
        failed = IngestOutcome(batch.source_system, batch.batch_id, status="rejected",
                               attempt=outcome.attempt,
                               rejection_reason=f"failed during apply: {exc}")
        with owner_transaction() as cur:
            _record_batch(cur, batch, failed, [])
        raise

    if outcome.status == "published":
        # Reclaim dead tuples the corrections and any offset shift left, now
        # that the new generation is committed. VACUUM cannot run inside a
        # transaction.
        with owner_transaction(autocommit=True) as cur:
            cur.execute("VACUUM (ANALYZE) sales")
            cur.execute("VACUUM (ANALYZE) app_ref.calendar")
        from app.analytics.entities import clear_caches

        clear_caches()
    return outcome


def _process(cur: Any, batch: SourceBatch, outcome: IngestOutcome,
             quarantine: list[tuple[SourceEvent | None, str]], today: date, tz: ZoneInfo,
             settings: Any) -> None:
    # -- 1. reconcile against the source's own control totals ---------------
    received_count = len(batch.events)
    received_packs = sum((_decimal(e.pack_units) for e in batch.events), Decimal(0))
    if received_count != batch.declared_count or received_packs != Decimal(batch.declared_pack_units):
        raise _Rejected(
            f"control totals do not reconcile: declared {batch.declared_count} events / "
            f"{batch.declared_pack_units} packs, received {received_count} / {received_packs}")

    # -- the published state this batch extends ------------------------------
    require_compatible(cur)
    cur.execute(
        "SELECT dataset_id, load_mode, source_hashes FROM app_meta.dataset_manifest "
        "WHERE load_state = 'published' ORDER BY published_at DESC LIMIT 1")
    parent = cur.fetchone()
    if parent is None:
        raise _Rejected("no published dataset to extend; run a full or seed load first")
    outcome.parent_dataset_id = parent["dataset_id"]
    cur.execute("SELECT wk_offset, period_wk, week_ending_date, mo_offset, period_mo, "
                "period_qtr FROM app_ref.calendar")
    weeks = [CalendarWeek(r["wk_offset"], r["period_wk"], date.fromisoformat(r["week_ending_date"]),
                          r["mo_offset"], r["period_mo"], r["period_qtr"])
             for r in cur.fetchall()]
    try:
        convention = detect_convention(weeks)
    except ConventionError as exc:
        raise _Rejected(f"the published calendar cannot be extended safely: {exc}") from None
    calendar = Calendar(weeks, convention)
    anchor_before = calendar.anchor.week_ending

    cur.execute("SELECT org_id FROM organizations")
    orgs = {r["org_id"] for r in cur.fetchall()}
    cur.execute("SELECT ndc FROM products")
    ndcs = {r["ndc"] for r in cur.fetchall()}

    # -- 2. validate and place ------------------------------------------------
    candidates: list[tuple[SourceEvent, str, Placement | None]] = []
    for event in batch.events:
        if (problem := _field_problem(event, orgs, ndcs, today, tz)) is not None:
            quarantine.append((event, problem))
            continue
        placement = None
        if event.kind == "upsert":
            placed = calendar.place(event.event_time.astimezone(tz).date())
            if isinstance(placed, str):
                quarantine.append((event, placed))
                continue
            placement = placed
        candidates.append((event, _digest(event), placement))

    # Same identity, same version, different content within one batch: the
    # source contradicts itself and there is no basis to pick one.
    seen: dict[tuple[str, int], str] = {}
    contradictory: set[tuple[str, int]] = set()
    for event, digest, _ in candidates:
        key = (event.source_event_id, event.event_version)
        if seen.setdefault(key, digest) != digest:
            contradictory.add(key)
    kept = []
    for event, digest, placement in candidates:
        if (event.source_event_id, event.event_version) in contradictory:
            quarantine.append((event, "conflicting_versions"))
        else:
            kept.append((event, digest, placement))

    # -- 3. classify against the ledger --------------------------------------
    ids = sorted({e.source_event_id for e, _, _ in kept})
    cur.execute(
        "SELECT source_event_id, event_version, payload_hash, sale_id, tombstoned "
        "FROM app_ingest.event_ledger WHERE source_system = %s AND source_event_id = ANY(%s) "
        "FOR UPDATE", (batch.source_system, ids))
    ledger = {r["source_event_id"]: dict(r) for r in cur.fetchall()}
    plan: list[tuple[str, SourceEvent, str, Placement | None]] = []
    state = {k: dict(v) for k, v in ledger.items()}
    for event, digest, placement in sorted(kept, key=lambda c: (c[0].source_event_id,
                                                                 c[0].event_version)):
        current = state.get(event.source_event_id)
        if current is not None and event.event_version < current["event_version"]:
            plan.append(("duplicate", event, digest, placement))
            continue
        if current is not None and event.event_version == current["event_version"]:
            if digest == current["payload_hash"]:
                plan.append(("duplicate", event, digest, placement))
            else:
                quarantine.append((event, "conflicting_versions"))
            continue
        if event.kind == "delete":
            action = "tombstone" if current is not None and not current["tombstoned"] \
                else "tombstone_only"
        else:
            action = "correct" if current is not None and not current["tombstoned"] else "insert"
        plan.append((action, event, digest, placement))
        state[event.source_event_id] = {"event_version": event.event_version,
                                        "payload_hash": digest,
                                        "tombstoned": event.kind == "delete"}

    outcome.quarantined = len(quarantine)
    for _, reason in quarantine:
        outcome.quarantine_reasons[reason] = outcome.quarantine_reasons.get(reason, 0) + 1

    # -- 4. quality threshold --------------------------------------------------
    limit = settings.ingest_max_quarantine_ratio
    if received_count and outcome.quarantined / received_count > limit:
        raise _Rejected(
            f"{outcome.quarantined} of {received_count} events failed validation, above the "
            f"{limit:.0%} limit; the feed is treated as broken and nothing is applied")

    # -- 5. apply ---------------------------------------------------------------
    cur.execute("SELECT coalesce(sum(pack_units::numeric), 0) AS total FROM sales")
    total_before = cur.fetchone()["total"]
    expected = Decimal(0)
    affected: set[str] = set()
    watermark: datetime | None = None
    changed = False

    for action, event, digest, placement in plan:
        if action == "duplicate":
            outcome.duplicates += 1
            continue
        entry = ledger.get(event.source_event_id)
        sale_id = entry["sale_id"] if entry else None
        old = None
        if sale_id is not None and action in ("correct", "tombstone"):
            cur.execute("SELECT pack_units::numeric AS packs, period_mo, wk_offset "
                        "FROM sales WHERE sale_id = %s FOR UPDATE", (sale_id,))
            old = cur.fetchone()
            if old is None:
                raise LoadError(f"ledger names sale {sale_id} for event "
                                f"{event.source_event_id}, but it does not exist")
            affected.add(old["period_mo"])
        if action == "insert":
            cur.execute(_SALES_INSERT, _fact_params(event, placement))
            sale_id = cur.fetchone()["sale_id"]
            expected += _decimal(event.pack_units)
            outcome.applied += 1
        elif action == "correct":
            cur.execute(_SALES_UPDATE, {**_fact_params(event, placement), "sale_id": sale_id})
            expected += _decimal(event.pack_units) - old["packs"]
            outcome.corrected += 1
        elif action == "tombstone":
            cur.execute("DELETE FROM sales WHERE sale_id = %s", (sale_id,))
            expected -= old["packs"]
            outcome.tombstoned += 1
            sale_id = None
        else:   # tombstone_only: nothing to remove, but a late copy must not resurrect it
            outcome.tombstoned += 1
            sale_id = None
        changed |= action != "tombstone_only"
        if placement is not None:
            affected.add(placement.period_mo)
            watermark = max(watermark, event.event_time) if watermark else event.event_time
            if placement.week_ending < anchor_before:
                outcome.late += 1
        elif old is not None and old["wk_offset"] > 0:
            outcome.late += 1
        cur.execute(
            "INSERT INTO app_ingest.event_ledger AS l (source_system, source_event_id, "
            " event_version, event_time, payload_hash, sale_id, tombstoned, first_batch_id, "
            " last_batch_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (source_system, source_event_id) DO UPDATE SET "
            " event_version = EXCLUDED.event_version, event_time = EXCLUDED.event_time, "
            " payload_hash = EXCLUDED.payload_hash, sale_id = EXCLUDED.sale_id, "
            " tombstoned = EXCLUDED.tombstoned, last_batch_id = EXCLUDED.last_batch_id, "
            " applied_at = now()",
            (batch.source_system, event.source_event_id, event.event_version,
             event.event_time or datetime.now(timezone.utc), digest, sale_id,
             event.kind == "delete", batch.batch_id, batch.batch_id))
        ledger[event.source_event_id] = {"sale_id": sale_id}

    outcome.affected_periods = sorted(affected)
    if watermark is not None:
        cur.execute(
            "INSERT INTO app_ingest.watermarks (source_system, watermark, last_batch_id, "
            " last_batch_at) VALUES (%s, %s, %s, now()) ON CONFLICT (source_system) DO UPDATE "
            "SET watermark = GREATEST(app_ingest.watermarks.watermark, EXCLUDED.watermark), "
            " last_batch_id = EXCLUDED.last_batch_id, last_batch_at = now()",
            (batch.source_system, watermark, batch.batch_id))
    else:
        cur.execute("UPDATE app_ingest.watermarks SET last_batch_id = %s, last_batch_at = now() "
                    "WHERE source_system = %s", (batch.batch_id, batch.source_system))

    if not changed:
        outcome.status = "no_change"
        return

    cur.execute("SELECT coalesce(sum(pack_units::numeric), 0) AS total FROM sales")
    actual = cur.fetchone()["total"] - total_before
    if actual != expected:
        raise LoadError(f"applied pack delta {actual} differs from the events' {expected}")
    outcome.pack_delta = expected

    # -- 6. offset 0 is the latest period present -------------------------------
    cur.execute("SELECT min(wk_offset) AS wk, min(mo_offset) AS mo FROM sales")
    low = cur.fetchone()
    if low["wk"] is None:
        raise LoadError("the batch would leave no sales at all")
    if low["wk"] or low["mo"]:
        cur.execute("UPDATE sales SET wk_offset = wk_offset - %s, mo_offset = mo_offset - %s",
                    (low["wk"], low["mo"]))
    outcome.anchor_shift_weeks = -low["wk"]
    outcome.anchor_shift_months = -low["mo"]

    # -- 7. calendar, validation, publication ------------------------------------
    dataset_id = f"{parent['load_mode']}-{uuid.uuid4().hex[:12]}"
    report = LoadReport(dataset_id=dataset_id, load_mode=parent["load_mode"])
    report.source_hashes = {k: v for k, v in (parent["source_hashes"] or {}).items()
                            if not k.startswith("batch:")}
    report.source_hashes[f"batch:{batch.source_system}/{batch.batch_id}"] = hashlib.sha256(
        "".join(_digest(e) for e in batch.events).encode()).hexdigest()
    cur.execute("DELETE FROM app_ref.calendar")
    _populate_calendar(cur, report)
    cur.execute("ANALYZE sales")
    _validate(cur, report)
    for table in ("sales", "organizations", "products", "zip_territory", "users"):
        cur.execute(f"SELECT count(*) AS n FROM {table}")
        report.row_counts[table] = cur.fetchone()["n"]

    cur.execute("UPDATE app_meta.dataset_manifest SET load_state = 'superseded' "
                "WHERE load_state = 'published'")
    cur.execute(
        "INSERT INTO app_meta.dataset_manifest (dataset_id, load_mode, load_state, "
        " schema_version, mapping_version, source_hashes, row_counts, reporting_anchor, "
        " source_coverage, warnings, published_at, parent_dataset_id, ingest_batch_id) "
        "VALUES (%s, %s, 'published', %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, "
        " %s::jsonb, now(), %s, %s)",
        (dataset_id, parent["load_mode"], "1.0.0", MAPPING_VERSION,
         _json(report.source_hashes), _json(report.row_counts), _json(report.reporting_anchor),
         _json(report.source_coverage), _json(report.warnings), parent["dataset_id"],
         f"{batch.source_system}/{batch.batch_id}"))
    cur.execute(
        "INSERT INTO app_ref.generation (singleton, dataset_id, published_at) "
        "VALUES (TRUE, %s, now()) ON CONFLICT (singleton) DO UPDATE "
        "SET dataset_id = EXCLUDED.dataset_id, published_at = EXCLUDED.published_at",
        (dataset_id,))
    outcome.dataset_id = dataset_id
    outcome.status = "published"


def _record_batch(cur: Any, batch: SourceBatch, outcome: IngestOutcome,
                  quarantine: list[tuple[SourceEvent | None, str]]) -> None:
    received_packs = sum((_decimal(e.pack_units) for e in batch.events), Decimal(0))
    cur.execute(
        "INSERT INTO app_ingest.batches AS b (source_system, batch_id, status, attempts, "
        " declared_count, declared_pack_units, received_count, received_pack_units, "
        " quarantined, applied, corrected, tombstoned, duplicates, late, affected_periods, "
        " anchor_shift_weeks, rejection_reason, dataset_id) "
        "VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (source_system, batch_id) DO UPDATE SET status = EXCLUDED.status, "
        " attempts = b.attempts + 1, last_attempt_at = now(), "
        " declared_count = EXCLUDED.declared_count, "
        " declared_pack_units = EXCLUDED.declared_pack_units, "
        " received_count = EXCLUDED.received_count, "
        " received_pack_units = EXCLUDED.received_pack_units, "
        " quarantined = EXCLUDED.quarantined, applied = EXCLUDED.applied, "
        " corrected = EXCLUDED.corrected, tombstoned = EXCLUDED.tombstoned, "
        " duplicates = EXCLUDED.duplicates, late = EXCLUDED.late, "
        " affected_periods = EXCLUDED.affected_periods, "
        " anchor_shift_weeks = EXCLUDED.anchor_shift_weeks, "
        " rejection_reason = EXCLUDED.rejection_reason, "
        " dataset_id = COALESCE(EXCLUDED.dataset_id, b.dataset_id)",
        (batch.source_system, batch.batch_id, outcome.status, batch.declared_count,
         batch.declared_pack_units, len(batch.events), received_packs, outcome.quarantined,
         outcome.applied, outcome.corrected, outcome.tombstoned, outcome.duplicates,
         outcome.late, outcome.affected_periods, outcome.anchor_shift_weeks,
         outcome.rejection_reason, outcome.dataset_id))
    for event, reason in quarantine:
        payload = event.payload() if event is not None else {}
        cur.execute(
            "INSERT INTO app_ingest.quarantine (source_system, batch_id, attempt, "
            " source_event_id, event_version, reason, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (batch.source_system, batch.batch_id, outcome.attempt,
             event.source_event_id if event else None,
             event.event_version if event else None, reason, json.dumps(payload, default=str)))
