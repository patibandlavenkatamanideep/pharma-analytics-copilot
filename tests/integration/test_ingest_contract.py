"""The ingestion input contract, enforced at the source boundary.

Review of 1 October 2026, R5. SourceEvent is a dataclass, so its type
annotations were never enforced. NaN and infinite prices and infinite
quantities passed validation -- and sales.pack_units and sales.wac are
DOUBLE PRECISION, which stores both, so they were PUBLISHED. A string
quantity raised TypeError instead of producing a reason. The JSON adapter
parsed timestamps and built events directly, so one malformed record failed
the whole file before quarantine could see it. A boolean or fractional
version reached the ledger, where PostgreSQL either refused it (failing the
batch) or rounded it (silently changing the identity). And a quarantined
payload containing NaN or a NUL character could not be stored as JSONB,
failing the batch it was meant to describe.

Every test feeds real JSON text through the real adapter (JsonBatchFiles)
into PostgreSQL. The batch-level quality threshold is lifted where a test
isolates what happens to one record; it is tested on its own.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.data.sources import SourceEvent
from tests.integration.test_ingestion import (  # noqa: F401  (fixtures)
    DISTRIBUTOR_NDC, IN_TERRITORY, NOW, NY, SOURCE, fresh, ingest_env, q,
)

R5 = pytest.mark.xfail(strict=True, reason="R5: non-finite, mistyped and malformed input "
                                           "is not caught at the source boundary")

#: JSON that json.dumps cannot write directly, spliced into the text.
RAW = {"@NaN@": "NaN", "@Infinity@": "Infinity", "@1e309@": "1e309", "@true@": "true"}


def event(event_id: str, *, when: str = "2026-09-18T10:00:00-04:00", version=1,
          packs=10, wac=1200.0, **fields) -> dict:
    return {"source_event_id": event_id, "event_version": version, "kind": "upsert",
            "event_time": when, "org_id": IN_TERRITORY, "ndc": DISTRIBUTOR_NDC,
            "data_source": "distributor", "pack_units": packs, "unit": "packs", "wac": wac,
            **fields}


def write(tmp_path, batch_id: str, events: list[dict], *, declared_count=None,
          declared_packs=None, **envelope) -> "pathlib.Path":  # noqa: F821
    if declared_packs is None:
        declared_packs = str(sum(e["pack_units"] for e in events
                                 if isinstance(e.get("pack_units"), (int, float))
                                 and not isinstance(e.get("pack_units"), bool)))
    doc = {"source_system": SOURCE, "batch_id": batch_id,
           "declared_count": len(events) if declared_count is None else declared_count,
           "declared_pack_units": declared_packs, "events": events, **envelope}
    text = json.dumps(doc)
    for marker, literal in RAW.items():
        text = text.replace(json.dumps(marker), literal)
    path = tmp_path / f"{batch_id}.json"
    path.write_text(text)
    return path


def ingest_file(path):
    from app.data.ingest import ingest
    from app.data.sources import JsonBatchFiles
    return [ingest(b, now=NOW) for b in JsonBatchFiles([path]).batches()]


@pytest.fixture
def per_record(monkeypatch):
    """Lift the batch quality threshold, to see what happens to one record."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ingest_max_quarantine_ratio", 1.0)


def non_finite_published() -> int:
    return q("SELECT count(*) AS n FROM sales WHERE wac::text IN ('NaN', 'Infinity', '-Infinity') "
             "OR pack_units::text IN ('NaN', 'Infinity', '-Infinity')")[0]["n"]


# -- reproductions ---------------------------------------------------------------------

@R5
def test_a_nan_price_is_quarantined_never_published(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "nan-1", [event("nan-ok"),
                                                      event("nan-bad", wac="@NaN@")]))
    assert non_finite_published() == 0
    assert outcome.status == "published" and outcome.applied == 1
    assert outcome.quarantine_reasons == {"non_finite_number": 1}


@R5
def test_an_infinite_quantity_is_never_published(fresh, per_record, tmp_path):
    """1e309 parses as infinity. With control totals that say so, it used
    to reconcile -- and publish."""
    [outcome] = ingest_file(write(tmp_path, "inf-1", [event("inf-ok"),
                                                      event("inf-bad", packs="@1e309@")],
                                  declared_packs="Infinity"))
    assert non_finite_published() == 0
    assert outcome.status == "rejected"


@R5
def test_a_numeric_string_quantity_is_a_reason_not_a_crash():
    from app.data.ingest import _field_problem
    bad = SourceEvent(source_event_id="s-1", event_version=1, kind="upsert",
                      event_time=NOW, org_id=IN_TERRITORY, ndc=DISTRIBUTOR_NDC,
                      data_source="distributor", pack_units="10", wac=1200.0)
    assert _field_problem(bad, {IN_TERRITORY}, {DISTRIBUTOR_NDC}, date(2026, 10, 10),
                          NY) == "invalid_type"


@R5
def test_a_malformed_timestamp_quarantines_that_event_not_the_file(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "ts-1", [
        event("ts-ok"), event("ts-bad", when="2026-13-45T99:00:00-04:00")]))
    assert outcome.status == "published" and outcome.applied == 1
    assert outcome.quarantine_reasons == {"invalid_timestamp": 1}


@R5
def test_a_boolean_version_is_quarantined_not_applied(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "bool-1", [event("bool-ok"),
                                                       event("bool-bad", version="@true@")]))
    assert outcome.status == "published" and outcome.applied == 1
    assert outcome.quarantine_reasons == {"invalid_identity": 1}


@R5
def test_a_fractional_version_is_quarantined_not_rounded(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "frac-1", [event("frac-ok"),
                                                       event("frac-bad", version=1.5)]))
    assert outcome.quarantine_reasons == {"invalid_identity": 1}
    assert q("SELECT event_version FROM app_ingest.event_ledger "
             "WHERE source_event_id = 'frac-bad'") == []


@R5
def test_a_quarantined_payload_that_json_cannot_store_is_still_recorded(fresh, per_record,
                                                                        tmp_path):
    """JSONB stores neither NaN nor a NUL character. The quarantine record is
    sanitised, not lost -- and its batch is not failed by it."""
    [outcome] = ingest_file(write(tmp_path, "nul-1", [
        event("nul-ok"), event("nul-bad", org_id="FA\u0000001", wac="@NaN@")]))
    assert outcome.status == "published" and outcome.applied == 1
    [row] = q("SELECT reason, payload FROM app_ingest.quarantine WHERE batch_id = 'nul-1'")
    assert row["reason"] == "non_finite_number"
    assert row["payload"]["wac"] == "NaN"
    assert "\u0000" not in json.dumps(row["payload"])
