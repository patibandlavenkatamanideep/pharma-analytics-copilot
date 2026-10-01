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
"""

from __future__ import annotations

import json
import pathlib
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Literal, Protocol


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
        return d


@dataclass(frozen=True)
class SourceBatch:
    source_system: str
    batch_id: str
    events: tuple[SourceEvent, ...]
    #: The source's control totals, over EVERY event it sent -- valid or not.
    declared_count: int
    declared_pack_units: Decimal

    @classmethod
    def of(cls, source_system: str, batch_id: str, events: Iterable[SourceEvent],
           **overrides: Any) -> "SourceBatch":
        events = tuple(events)
        totals = {"declared_count": len(events),
                  "declared_pack_units": sum((Decimal(str(e.pack_units or 0)) for e in events),
                                             Decimal(0))}
        totals.update(overrides)
        return cls(source_system=source_system, batch_id=batch_id, events=events, **totals)


class SourceAdapter(Protocol):
    source_system: str

    def batches(self) -> Iterable[SourceBatch]: ...


@dataclass
class JsonBatchFiles:
    """Batches delivered as JSON documents, one per file, in name order:

        {"source_system": "...", "batch_id": "...",
         "declared_count": 2, "declared_pack_units": "35",
         "events": [{"source_event_id": "...", "event_version": 1,
                     "kind": "upsert", "event_time": "2026-09-24T10:00:00-04:00",
                     "org_id": "...", "ndc": "...", "data_source": "distributor",
                     "pack_units": 10, "unit": "packs", "wac": 1200.0}, ...]}

    A timestamp without an offset is parsed as naive and quarantined by
    ingestion -- never assumed to be UTC or local time.
    """

    paths: list[pathlib.Path]
    source_system: str = "file"

    def batches(self) -> Iterable[SourceBatch]:
        for path in sorted(self.paths):
            doc = json.loads(path.read_text())
            events = []
            for raw in doc["events"]:
                raw = dict(raw)
                when = raw.get("event_time")
                raw["event_time"] = datetime.fromisoformat(when) if when else None
                events.append(SourceEvent(**raw))
            yield SourceBatch(source_system=doc["source_system"], batch_id=doc["batch_id"],
                              events=tuple(events), declared_count=int(doc["declared_count"]),
                              declared_pack_units=Decimal(str(doc["declared_pack_units"])))


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
