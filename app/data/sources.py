"""Where incremental data comes from: a source-adapter interface, and a
replayable synthetic source that exercises the real ingestion path.

A source delivers BATCHES of EVENTS. Every event carries the identity the
ledger deduplicates on -- (source_system, source_event_id) -- and a version,
so a correction is a newer version of the same event rather than a new sale,
and a deletion is a tombstone rather than an absence. A batch carries the
source's own control totals (how many events, how many packs), against which
what actually arrived is reconciled before anything is published.

Production sources (a distributor feed, a market-data drop) implement
SourceAdapter. The synthetic source exists so the ingestion path can be run
end to end without production access; it is deterministic for a given seed,
so a replay is byte-for-byte the same batch.

The contract is enforced where data arrives (review of 1 October 2026, R5):
a dataclass does not check its annotations, so a NaN price, an infinite
quantity, a string where a number belongs, a boolean or fractional version
or an unparseable timestamp used to travel as far as SQL. parse_batch reads
a JSON document strictly and separates two kinds of failure:

* **Envelope errors reject the whole batch** -- nothing in it is applied,
  and the rejection is recorded with a stable code: `malformed_document`
  (not JSON, not UTF-8, not an object, or no usable source/batch identity)
  and `invalid_envelope` (a missing, mistyped or non-finite control total,
  an unexpected top-level field, a duplicate key, no event list, or more
  than MAX_EVENTS_PER_BATCH events).
* **Record errors quarantine one event** -- with a stable reason, the rest
  of the batch going ahead (subject to the batch quality threshold):
  `malformed_record`, `unexpected_field`, `invalid_identity`,
  `invalid_type`, `invalid_timestamp`, `non_finite_number`, plus the
  semantic reasons app/data/ingest.py adds.

A record that cannot be read still counts toward reconciliation: the
source's control totals cover everything it sent. Its pack quantity counts
if it reads as a finite decimal (a number or a numeric string) and as zero
otherwise, so totals that include an unreadable value do not reconcile and
the batch is refused.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import pathlib
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Literal, Protocol

#: Sanity bounds. No real sale comes near them; a value beyond one is a unit
#: or parsing error, quarantined as `out_of_range` rather than published.
MAX_PACKS_PER_EVENT = 1_000_000
MAX_WAC_PER_PACK = 10_000_000
MAX_EVENT_VERSION = 2**31 - 1          # the ledger's INTEGER column
MAX_IDENTITY_LENGTH = 256
MAX_EVENTS_PER_BATCH = 100_000

RECORD_REASONS = frozenset({
    "malformed_record", "unexpected_field", "invalid_identity", "invalid_type",
    "invalid_timestamp", "non_finite_number", "out_of_range", "invalid_kind",
    "missing_field", "naive_timestamp", "unit_not_packs", "unknown_data_source",
    "unknown_organization", "unknown_product", "non_positive_packs", "invalid_price",
    "priced_free_drug", "future_event", "before_history",
    "period_convention_ambiguous", "calendar_conflict", "conflicting_versions",
})


@dataclass(frozen=True)
class SourceEvent:
    source_event_id: str
    event_version: int
    kind: Literal["upsert", "delete"]
    #: When the sale happened, timezone-aware. Converted to a business date in
    #: the configured business timezone, never by dropping the offset.
    event_time: datetime | None
    org_id: str | None = None
    ndc: str | None = None
    data_source: str | None = None
    pack_units: float | None = None
    #: Units are stated, not assumed. Only "packs" is accepted; anything else
    #: is quarantined rather than converted silently.
    unit: str | None = "packs"
    wac: float | None = None

    def payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_time"] = self.event_time.isoformat() if self.event_time else None
        for name in ("pack_units", "wac"):
            if isinstance(d[name], Decimal):
                d[name] = float(d[name])
        return d


@dataclass(frozen=True)
class Malformed:
    """A record that could not be read as an event: quarantined with its
    reason, and still counted toward reconciliation."""

    reason: str
    #: What arrived, made storable (storable()): never NaN, never a NUL.
    payload: dict[str, Any]
    source_event_id: str | None = None
    event_version: int | None = None
    #: The record's pack quantity where it reads as a finite decimal, else 0.
    packs: Decimal = Decimal(0)


@dataclass(frozen=True)
class SourceBatch:
    source_system: str
    batch_id: str
    events: tuple[SourceEvent, ...]
    #: The source's control totals, over EVERY event it sent -- valid or not.
    declared_count: int
    declared_pack_units: Decimal
    #: Records that could not be read as events.
    malformed: tuple[Malformed, ...] = ()
    #: (code, detail) when the batch itself is unusable: rejected whole.
    envelope_error: tuple[str, str] | None = None

    @classmethod
    def of(cls, source_system: str, batch_id: str, events: Iterable[SourceEvent],
           **overrides: Any) -> "SourceBatch":
        events = tuple(events)
        totals = {"declared_count": len(events),
                  "declared_pack_units": sum((readable_packs(e.pack_units) for e in events),
                                             Decimal(0))}
        totals.update(overrides)
        return cls(source_system=source_system, batch_id=batch_id, events=events, **totals)


class SourceAdapter(Protocol):
    source_system: str

    def batches(self) -> Iterable[SourceBatch]: ...


# ---------------------------------------------------------------------------
# Reading JSON strictly
# ---------------------------------------------------------------------------

EVENT_FIELDS = frozenset(f.name for f in dataclasses.fields(SourceEvent))
ENVELOPE_FIELDS = frozenset({"source_system", "batch_id", "declared_count",
                             "declared_pack_units", "events"})
_SOURCE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_BATCH_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")


class _NonFinite(str):
    """A NaN or Infinity literal, kept as its text so it can be reported."""


class _Object(dict):
    duplicate = False


def _object(pairs: list[tuple[str, Any]]) -> _Object:
    out = _Object()
    for key, value in pairs:
        if key in out:
            out.duplicate = True
        out[key] = value
    return out


def _load(text: str) -> Any:
    """JSON with nothing lost: decimals as Decimal (so 1e309 is a number too
    large, not infinity), NaN/Infinity kept as marked text, duplicate keys
    noticed."""
    return json.loads(text, parse_float=Decimal, parse_constant=_NonFinite,
                      object_pairs_hook=_object)


def readable_packs(value: Any) -> Decimal:
    """A pack quantity for reconciliation: what reads as a finite decimal a
    DOUBLE PRECISION column could hold (a number or a numeric string), or
    zero -- the same line non_finite_number draws for a record. Never
    raises."""
    if isinstance(value, bool) or value is None or isinstance(value, _NonFinite):
        return Decimal(0)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if (not number.is_finite() or not math.isfinite(float(number))
                or len(number.as_tuple().digits) > 1000
                or (number and number.adjusted() < -324)):
            return Decimal(0)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return Decimal(0)
    return number


def valid_total(value: Any) -> bool:
    try:
        number = Decimal(str(value))
        # Control totals are NUMERIC in PostgreSQL, with finite application
        # bounds matching the largest accepted batch. Prevent extreme exponents.
        return (number.is_finite() and len(number.as_tuple().digits) <= 1000
                and (not number or number.adjusted() >= -324)
                and 0 <= number <= MAX_EVENTS_PER_BATCH * MAX_PACKS_PER_EVENT)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return False


def normalize_batch(batch: SourceBatch) -> SourceBatch:
    """Enforce the envelope at every adapter boundary, before SQL or telemetry.
    Rejected envelopes use safe identity/control fields so the rejection itself
    can be stored. No accepted event's established digest representation changes.
    """
    source, bid = batch.source_system, batch.batch_id
    identity_ok = (isinstance(source, str) and _SOURCE_ID.fullmatch(source)
                   and isinstance(bid, str) and _BATCH_ID.fullmatch(bid))
    if not identity_ok:
        raw = json.dumps(storable([source, bid]), ensure_ascii=True).encode()
        return SourceBatch("invalid-adapter", "unreadable-" + hashlib.sha256(raw).hexdigest()[:16],
                           (), 0, Decimal(0), envelope_error=("malformed_document", "invalid batch identity"))
    if (type(batch.declared_count) is not int or not 0 <= batch.declared_count <= MAX_EVENT_VERSION
            or not valid_total(batch.declared_pack_units)
            or not isinstance(batch.events, (list, tuple))
            or not isinstance(batch.malformed, (list, tuple))
            or len(batch.events) + len(batch.malformed) > MAX_EVENTS_PER_BATCH
            or any(not isinstance(e, SourceEvent) for e in batch.events)
            or any(not isinstance(m, Malformed) for m in batch.malformed)):
        return SourceBatch(source, bid, (), 0, Decimal(0),
                           envelope_error=("invalid_envelope", "invalid adapter control fields"))
    malformed = tuple(dataclasses.replace(m,
        reason=m.reason if isinstance(m.reason, str) and m.reason in RECORD_REASONS else "malformed_record",
        payload=storable(m.payload),
        source_event_id=_text(m.source_event_id)[:MAX_IDENTITY_LENGTH] if isinstance(m.source_event_id, str) else None,
        event_version=m.event_version if type(m.event_version) is int and 1 <= m.event_version <= MAX_EVENT_VERSION else None,
        packs=readable_packs(m.packs)) for m in batch.malformed)
    error = batch.envelope_error
    if error is not None:
        code = error[0] if isinstance(error, (tuple, list)) and error else None
        error = (code if code in ("malformed_document", "invalid_envelope") else "invalid_envelope",
                 "source envelope rejected")
    return dataclasses.replace(batch, malformed=malformed, envelope_error=error)


def _number(value: Any) -> tuple[Any, str | None]:
    """A JSON number as the Python value json.loads would have made of it
    (int, or float), or why it is not one. Same values, same event digests:
    a replay of a batch read before this parser is still a duplicate."""
    if value is None:
        return None, None
    if isinstance(value, _NonFinite):
        return None, "non_finite_number"
    if isinstance(value, bool) or isinstance(value, str) or not isinstance(value, (int, Decimal)):
        return None, "invalid_type"
    try:
        as_float = float(value)
    except OverflowError:
        return None, "non_finite_number"
    if not math.isfinite(as_float):
        return None, "non_finite_number"
    return (value if isinstance(value, int) else as_float), None


def _text(value: str) -> str:
    """Text PostgreSQL can store: no NUL, no lone surrogate, bounded."""
    return value.replace("\x00", "\ufffd").encode("utf-8", "replace").decode("utf-8")[:1_000]


def storable(value: Any, depth: int = 0) -> Any:
    """Any value as JSON that JSONB accepts. Non-finite numbers become their
    names, NUL characters and lone surrogates are replaced, and size and
    depth are bounded -- so a malformed record is recorded, not lost, and
    never fails the batch that carries it."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, _NonFinite):
        return str(value)
    if isinstance(value, int):
        if value.bit_length() > 4096:
            return "[integer out of range]"
        return value if abs(value) < 2**63 else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else ("NaN" if math.isnan(value)
                                                   else "Infinity" if value > 0 else "-Infinity")
    if isinstance(value, Decimal):
        if value.is_nan():
            return "NaN"
        if not value.is_finite():
            return "Infinity" if value > 0 else "-Infinity"
        as_float = float(value)
        return as_float if math.isfinite(as_float) else str(value)
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if depth >= 6:
        return "[nested too deep]"
    if isinstance(value, dict):
        return {_text(str(k))[:100]: storable(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [storable(v, depth + 1) for v in value[:50]]
    return _text(repr(value))[:200]


def parse_event(raw: Any) -> "SourceEvent | Malformed":
    """One record from a JSON batch, read strictly. Semantic checks (known
    organization, unit, price rules, calendar) follow in app/data/ingest.py."""
    if not isinstance(raw, dict):
        return Malformed("malformed_record", {"record": storable(raw)})
    sid, version = raw.get("source_event_id"), raw.get("event_version")
    payload = storable(raw)

    def bad(reason: str) -> Malformed:
        return Malformed(reason, payload,
                         source_event_id=_text(sid)[:MAX_IDENTITY_LENGTH] if isinstance(sid, str) else None,
                         event_version=version if type(version) is int
                         and 1 <= version <= MAX_EVENT_VERSION else None,
                         packs=readable_packs(raw.get("pack_units")))

    if getattr(raw, "duplicate", False):
        return bad("malformed_record")
    if set(raw) - EVENT_FIELDS:
        return bad("unexpected_field")
    if not isinstance(sid, str) or not sid or len(sid) > MAX_IDENTITY_LENGTH \
            or _CONTROL.search(sid):
        return bad("invalid_identity")
    if type(version) is not int or not 1 <= version <= MAX_EVENT_VERSION:
        return bad("invalid_identity")
    for name in ("kind", "org_id", "ndc", "data_source", "unit"):
        if raw.get(name) is not None and not isinstance(raw[name], str):
            return bad("invalid_type")
    when = raw.get("event_time")
    if when is not None:
        if not isinstance(when, str):
            return bad("invalid_timestamp")
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return bad("invalid_timestamp")
    numbers = {}
    for name in ("pack_units", "wac"):
        value, problem = _number(raw.get(name))
        if problem:
            return bad(problem)
        numbers[name] = value
    if any(isinstance(raw.get(name), str) and _CONTROL.search(raw[name])
           for name in ("kind", "org_id", "ndc", "data_source", "unit")):
        return bad("invalid_type")
    # Fields named explicitly: a record without `kind` is an event with no
    # kind (ingestion's invalid_kind), not a TypeError from the dataclass.
    return SourceEvent(source_event_id=sid, event_version=version, kind=raw.get("kind"),
                       event_time=when,
                       **{k: raw[k] for k in ("org_id", "ndc", "data_source", "unit") if k in raw},
                       **{k: v for k, v in numbers.items() if k in raw})


def parse_batch(data: bytes, *, default_source: str = "file") -> SourceBatch:
    """A JSON batch document, read strictly (see the module docstring). Never
    raises for bad input: an unusable document is a batch with an envelope
    error, which ingestion records as rejected. One the document cannot
    name is recorded under `default_source` and a digest of its bytes, so
    resending the same broken file is the same rejected batch."""
    fallback = {"source_system": default_source if isinstance(default_source, str) and _SOURCE_ID.fullmatch(default_source) else "file",
                "batch_id": "unreadable-" + hashlib.sha256(data).hexdigest()[:16]}

    def rejected(code: str, detail: str, ident: dict[str, str] = fallback) -> SourceBatch:
        return SourceBatch(**ident, events=(), declared_count=0, declared_pack_units=Decimal(0),
                           envelope_error=(code, detail))

    try:
        doc = _load(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError, InvalidOperation, OverflowError):
        return rejected("malformed_document", "the document is not valid UTF-8 JSON")
    if not isinstance(doc, dict):
        return rejected("malformed_document", "the document is not a JSON object")
    source, batch_id = doc.get("source_system"), doc.get("batch_id")
    if not (isinstance(source, str) and _SOURCE_ID.fullmatch(source)
            and isinstance(batch_id, str) and _BATCH_ID.fullmatch(batch_id)):
        return rejected("malformed_document", "source_system or batch_id is missing or invalid")
    ident = {"source_system": source, "batch_id": batch_id}
    if doc.duplicate:
        return rejected("invalid_envelope", "a top-level key appears twice", ident)
    if set(doc) - ENVELOPE_FIELDS:
        return rejected("invalid_envelope", "unexpected top-level field", ident)
    count = doc.get("declared_count")
    if type(count) is not int or not 0 <= count <= MAX_EVENT_VERSION:
        return rejected("invalid_envelope", "declared_count is not a non-negative integer", ident)
    declared_raw = doc.get("declared_pack_units")
    declared = None
    if not isinstance(declared_raw, (bool, _NonFinite)) and declared_raw is not None:
        try:
            declared = Decimal(str(declared_raw))
        except InvalidOperation:
            declared = None
    if not valid_total(declared):
        return rejected("invalid_envelope",
                        "declared_pack_units is not a finite, non-negative number", ident)
    events = doc.get("events")
    if not isinstance(events, list):
        return rejected("invalid_envelope", "events is not a list", ident)
    if len(events) > MAX_EVENTS_PER_BATCH:
        return rejected("invalid_envelope", f"more than {MAX_EVENTS_PER_BATCH} events", ident)
    parsed = [parse_event(raw) for raw in events]
    return SourceBatch(**ident, events=tuple(e for e in parsed if isinstance(e, SourceEvent)),
                       declared_count=count, declared_pack_units=declared,
                       malformed=tuple(m for m in parsed if isinstance(m, Malformed)))


@dataclass
class JsonBatchFiles:
    """Batches delivered as JSON documents, one per file, in name order:

        {"source_system": "...", "batch_id": "...",
         "declared_count": 2, "declared_pack_units": "35",
         "events": [{"source_event_id": "...", "event_version": 1,
                     "kind": "upsert", "event_time": "2026-09-24T10:00:00-04:00",
                     "org_id": "...", "ndc": "...", "data_source": "distributor",
                     "pack_units": 10, "unit": "packs", "wac": 1200.0}, ...]}

    Read by parse_batch: a document that breaks the contract is a rejected
    batch and a record that breaks it is a quarantined event, never an
    exception. A timestamp without an offset is parsed as naive and
    quarantined by ingestion -- never assumed to be UTC or local time.
    """

    paths: list[pathlib.Path]
    source_system: str = "file"

    def batches(self) -> Iterable[SourceBatch]:
        for path in sorted(self.paths):
            yield parse_batch(path.read_bytes(), default_source=self.source_system)


@dataclass
class SyntheticIncrementalSource:
    """Deterministic batches drawn from the organizations and products that
    exist, with the awkward cases a real feed produces.

    `orgs` and `ndcs` are ids read from the database by the caller;
    `latest_week_ending` anchors event times so a batch can extend the
    reporting calendar or fall inside it.
    """

    orgs: list[str]
    ndcs: list[str]
    latest_week_ending: datetime
    seed: int = 7
    source_system: str = "synthetic-distributor"
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def _event(self, event_id: str, *, version: int = 1, days_before_anchor: int = 2,
               packs: float | None = None, **overrides: Any) -> SourceEvent:
        rng = self._rng
        when = self.latest_week_ending - timedelta(days=days_before_anchor, hours=rng.randint(1, 20))
        base = dict(
            source_event_id=event_id, event_version=version, kind="upsert", event_time=when,
            org_id=rng.choice(self.orgs), ndc=rng.choice(self.ndcs), data_source="distributor",
            pack_units=packs if packs is not None else float(rng.randint(1, 40)),
            unit="packs", wac=round(rng.uniform(100, 5000), 2),
        )
        base.update(overrides)
        return SourceEvent(**base)

    def new_sales(self, batch_id: str, n: int, *, prefix: str = "evt") -> SourceBatch:
        return SourceBatch.of(self.source_system, batch_id,
                              [self._event(f"{prefix}-{batch_id}-{i}") for i in range(n)])

    def batch(self, batch_id: str, events: Iterable[SourceEvent], **overrides: Any) -> SourceBatch:
        return SourceBatch.of(self.source_system, batch_id, events, **overrides)

    def event(self, event_id: str, **kw: Any) -> SourceEvent:
        return self._event(event_id, **kw)

    def batches(self) -> Iterable[SourceBatch]:
        """A short, replayable feed: new sales, then corrections and a
        deletion of some of them, then a late arrival."""
        first = self.new_sales("b1", 20)
        yield first
        corrected = [SourceEvent(**{**asdict(e), "event_version": 2,
                                    "pack_units": (e.pack_units or 0) + 1})
                     for e in first.events[:3]]
        deleted = SourceEvent(**{**asdict(first.events[3]), "event_version": 2, "kind": "delete"})
        yield self.batch("b2", [*corrected, deleted])
        yield self.batch("b3", [self._event("late-b3-0", days_before_anchor=60)])


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
