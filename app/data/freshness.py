"""How old each source's data is now, not when its last batch landed.

Review of 1 October 2026, R4. The only freshness signal was
`pac.ingest.lag`, set when a batch is applied. A feed that delivers one
healthy batch and then stops leaves that value where it was: the alert
watching it never fires. Freshness has to be computed at the moment it is
read, from what was persisted, so that silence makes it worse on its own.

Two measurements, kept separate because they fail differently:

* **since_success** -- seconds since the last batch this source delivered
  that was accepted (published, or no change). It grows when the feed or
  its schedule stops, whatever the data says. "Missed runs" are this
  against the expected interval.
* **watermark_age** -- seconds since the newest event time ever applied for
  the source. It grows when the data stops moving forward, even if batches
  keep arriving (an empty or stale feed).

Neither is publication delay, which is how long applying a batch takes
(`pac.ingest.duration`). Both are read from app_ingest.watermarks, written
in the same transaction that publishes a batch, using the database's clock
for both ends.

Read by the serving role (it has SELECT on app_ingest.watermarks) for the
gauges a running API exports at every collection, and by the jobs process
for `scripts/ingest.py --check-freshness`, which a scheduler can run on its
own -- so a stopped feed is visible without a running collector, and the
absence of the gauges is itself alertable (docs/OBSERVABILITY.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    last_success_at: datetime
    watermark: datetime
    checked_at: datetime

    @property
    def since_success_s(self) -> float:
        return max((self.checked_at - self.last_success_at).total_seconds(), 0.0)

    @property
    def watermark_age_s(self) -> float:
        return max((self.checked_at - self.watermark).total_seconds(), 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "last_success_at": self.last_success_at.isoformat(),
                "watermark": self.watermark.isoformat(),
                "checked_at": self.checked_at.isoformat(),
                "since_success_s": round(self.since_success_s, 1),
                "watermark_age_s": round(self.watermark_age_s, 1)}


def read(transaction: Callable[[], Any] | None = None) -> list[SourceFreshness]:
    """Every source that has ever delivered an accepted batch. A short
    statement timeout: this runs on a metrics thread and must not hang it."""
    if transaction is None:
        from app.db import freshness_transaction as transaction
    with transaction() as cur:
        cur.execute("SET LOCAL statement_timeout = '500ms'")
        cur.execute("SELECT source_system, watermark, last_batch_at, now() AS checked_at "
                    "FROM app_ingest.watermarks ORDER BY source_system")
        rows = cur.fetchall()
    return [SourceFreshness(r["source_system"], r["last_batch_at"], r["watermark"],
                            r["checked_at"]) for r in rows]


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def duration(text: str) -> timedelta:
    """'90m', '26h', '3d' -> timedelta."""
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError(f"not a duration: {text!r} (use e.g. 90m, 26h, 3d)")
    return timedelta(seconds=float(match.group(1)) * _UNIT[match.group(2)])


def problems(found: list[SourceFreshness], *, expected: list[str] | None = None,
             max_since_success: timedelta | None = None,
             max_watermark_age: timedelta | None = None) -> list[dict[str, Any]]:
    """What is stale, as stable codes: `never_delivered` (an expected
    source with no accepted batch), `missed_run` (no accepted batch within
    max_since_success), `stale_data` (newest event older than
    max_watermark_age). No source at all is itself a problem."""
    out: list[dict[str, Any]] = []
    by_source = {f.source: f for f in found}
    for source in expected or []:
        if source not in by_source:
            out.append({"source": source, "problem": "never_delivered"})
    if not found and not expected:
        out.append({"source": None, "problem": "never_delivered"})
    for f in found:
        if max_since_success is not None and f.since_success_s > max_since_success.total_seconds():
            out.append({"source": f.source, "problem": "missed_run",
                        "since_success_s": round(f.since_success_s, 1)})
        if max_watermark_age is not None and f.watermark_age_s > max_watermark_age.total_seconds():
            out.append({"source": f.source, "problem": "stale_data",
                        "watermark_age_s": round(f.watermark_age_s, 1)})
    return out
