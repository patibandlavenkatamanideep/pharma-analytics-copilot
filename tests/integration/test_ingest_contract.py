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

The first seven reproduced it on the unmodified code
(evidence/runs/r3-r5-reproduced.json). Every test feeds real JSON text
through the real adapter (JsonBatchFiles) into PostgreSQL. The batch-level quality threshold is lifted where a test
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

def test_a_nan_price_is_quarantined_never_published(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "nan-1", [event("nan-ok"),
                                                      event("nan-bad", wac="@NaN@")]))
    assert non_finite_published() == 0
    assert outcome.status == "published" and outcome.applied == 1
    assert outcome.quarantine_reasons == {"non_finite_number": 1}


def test_an_infinite_quantity_is_never_published(fresh, per_record, tmp_path):
    """1e309 parses as infinity. With control totals that say so, it used
    to reconcile -- and publish."""
    [outcome] = ingest_file(write(tmp_path, "inf-1", [event("inf-ok"),
                                                      event("inf-bad", packs="@1e309@")],
                                  declared_packs="Infinity"))
    assert non_finite_published() == 0
    assert outcome.status == "rejected"


def test_a_numeric_string_quantity_is_a_reason_not_a_crash():
    from app.data.ingest import _field_problem
    bad = SourceEvent(source_event_id="s-1", event_version=1, kind="upsert",
                      event_time=NOW, org_id=IN_TERRITORY, ndc=DISTRIBUTOR_NDC,
                      data_source="distributor", pack_units="10", wac=1200.0)
    assert _field_problem(bad, {IN_TERRITORY}, {DISTRIBUTOR_NDC}, date(2026, 10, 10),
                          NY) == "invalid_type"


def test_a_malformed_timestamp_quarantines_that_event_not_the_file(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "ts-1", [
        event("ts-ok"), event("ts-bad", when="2026-13-45T99:00:00-04:00")]))
    assert outcome.status == "published" and outcome.applied == 1
    assert outcome.quarantine_reasons == {"invalid_timestamp": 1}


def test_a_boolean_version_is_quarantined_not_applied(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "bool-1", [event("bool-ok"),
                                                       event("bool-bad", version="@true@")]))
    assert outcome.status == "published" and outcome.applied == 1
    assert outcome.quarantine_reasons == {"invalid_identity": 1}


def test_a_fractional_version_is_quarantined_not_rounded(fresh, per_record, tmp_path):
    [outcome] = ingest_file(write(tmp_path, "frac-1", [event("frac-ok"),
                                                       event("frac-bad", version=1.5)]))
    assert outcome.quarantine_reasons == {"invalid_identity": 1}
    assert q("SELECT event_version FROM app_ingest.event_ledger "
             "WHERE source_event_id = 'frac-bad'") == []


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


# -- envelope errors: the whole batch is refused, and nothing changes -----------------

def snapshot():
    from tests.integration.test_ingestion import state
    return {**state(),
            "ledger": q("SELECT count(*) AS n FROM app_ingest.event_ledger")[0]["n"],
            "watermarks": q("SELECT count(*) AS n FROM app_ingest.watermarks")[0]["n"]}


def raw_file(tmp_path, name: str, data: bytes):
    path = tmp_path / f"{name}.json"
    path.write_bytes(data)
    return path


VALID = json.dumps(event("env-ok"))


@pytest.mark.parametrize("name,document,code", [
    ("not-json", b'{"source_system": "x", ', "malformed_document"),
    ("not-utf8", b'\xff\xfe{"a": 1}', "malformed_document"),
    ("a-list", b'[1, 2, 3]', "malformed_document"),
    ("no-batch-id", b'{"source_system": "test-feed", "declared_count": 0, '
                    b'"declared_pack_units": "0", "events": []}', "malformed_document"),
    ("deeply-nested", b'{"source_system": "test-feed", "batch_id": "deep", "events": '
                      + b"[" * 100_000 + b"]" * 100_000 + b"}", "malformed_document"),
    ("dup-key", ('{"source_system": "test-feed", "batch_id": "env-dup", "declared_count": 1, '
                 '"declared_count": 1, "declared_pack_units": "10", "events": [' + VALID + ']}'
                 ).encode(), "invalid_envelope"),
    ("extra-field", ('{"source_system": "test-feed", "batch_id": "env-extra", "declared_count": 1, '
                     '"declared_pack_units": "10", "priority": "high", "events": [' + VALID + ']}'
                     ).encode(), "invalid_envelope"),
    ("bool-count", ('{"source_system": "test-feed", "batch_id": "env-bool", "declared_count": true, '
                    '"declared_pack_units": "10", "events": [' + VALID + ']}').encode(),
     "invalid_envelope"),
    ("string-count", ('{"source_system": "test-feed", "batch_id": "env-str", "declared_count": "1", '
                      '"declared_pack_units": "10", "events": [' + VALID + ']}').encode(),
     "invalid_envelope"),
    ("nan-total", ('{"source_system": "test-feed", "batch_id": "env-nan", "declared_count": 1, '
                   '"declared_pack_units": NaN, "events": [' + VALID + ']}').encode(),
     "invalid_envelope"),
    ("negative-total", ('{"source_system": "test-feed", "batch_id": "env-neg", "declared_count": 1, '
                        '"declared_pack_units": -10, "events": [' + VALID + ']}').encode(),
     "invalid_envelope"),
    ("events-object", b'{"source_system": "test-feed", "batch_id": "env-obj", "declared_count": 0, '
                      b'"declared_pack_units": "0", "events": {}}', "invalid_envelope"),
])
def test_an_unusable_batch_is_rejected_whole_with_a_stable_code(fresh, tmp_path, name,
                                                                 document, code):
    before = snapshot()
    [outcome] = ingest_file(raw_file(tmp_path, name, document))
    assert (outcome.status, outcome.rejection_code) == ("rejected", code)
    assert snapshot() == before, "a rejected batch changed published data"
    [row] = q("SELECT status, rejection_code FROM app_ingest.batches WHERE batch_id = %s",
              (outcome.batch_id,))
    assert (row["status"], row["rejection_code"]) == ("rejected", code)
    assert q("SELECT count(*) AS n FROM app_ingest.quarantine")[0]["n"] == 0


def test_more_events_than_a_batch_may_carry_is_an_envelope_error(fresh, tmp_path, monkeypatch):
    import app.data.sources as sources
    monkeypatch.setattr(sources, "MAX_EVENTS_PER_BATCH", 2)
    [outcome] = ingest_file(write(tmp_path, "too-many", [event(f"m-{i}") for i in range(3)]))
    assert (outcome.status, outcome.rejection_code) == ("rejected", "invalid_envelope")


def test_resending_the_same_broken_file_is_the_same_rejected_batch(fresh, tmp_path):
    broken = raw_file(tmp_path, "broken", b"not json at all")
    first, = ingest_file(broken)
    again, = ingest_file(broken)
    assert first.batch_id == again.batch_id and first.batch_id.startswith("unreadable-")
    [row] = q("SELECT attempts FROM app_ingest.batches WHERE batch_id = %s", (first.batch_id,))
    assert row["attempts"] == 2


# -- record errors: one event quarantined, with a stable reason -----------------------

@pytest.mark.parametrize("bad,reason", [
    (event("r-nan", wac="@NaN@"), "non_finite_number"),
    (event("r-ninf", wac="-Infinity"), "invalid_type"),          # a string, not a literal
    (event("r-inf", wac="@Infinity@"), "non_finite_number"),
    (event("r-big", wac="@1e309@"), "non_finite_number"),
    (event("r-bigpacks", packs="@1e309@"), "non_finite_number"),
    (event("r-strpacks", packs="10"), "invalid_type"),
    (event("r-boolpacks", packs="@true@"), "invalid_type"),
    (event("r-boolwac", wac="@true@"), "invalid_type"),
    (event("r-strwac", wac="1200"), "invalid_type"),
    (event("r-strversion", version="1"), "invalid_identity"),
    (event("r-zero", version=0), "invalid_identity"),
    (event("r-huge-version", version=2**31), "invalid_identity"),
    (event("r-intid", source_event_id=12345), "invalid_identity"),
    (event("r-nulid", source_event_id="r-\u0000-id"), "invalid_identity"),
    (event("x" * 300), "invalid_identity"),
    (event("r-month", when="2026-13-01T10:00:00-04:00"), "invalid_timestamp"),
    (event("r-empty", when=""), "invalid_timestamp"),
    (event("r-word", when="yesterday"), "invalid_timestamp"),
    (event("r-numtime", when=1_790_000_000), "invalid_timestamp"),
    (event("r-naive", when="2026-09-18T10:00:00"), "naive_timestamp"),
    (event("r-extra", discount=5), "unexpected_field"),
    (event("r-intorg", org_id=17), "invalid_type"),
    (event("r-packs", packs=2_000_000), "out_of_range"),
    (event("r-price", wac=20_000_000.0), "out_of_range"),
    (event("r-nokind", kind=None), "invalid_kind"),
])
def test_a_malformed_record_is_quarantined_with_its_reason_and_the_rest_applied(
        fresh, per_record, tmp_path, bad, reason):
    good = event("r-good")
    readable = bad.get("pack_units") if isinstance(bad.get("pack_units"), (int, float)) \
        and not isinstance(bad.get("pack_units"), bool) else 0
    if bad.get("pack_units") == "10":
        readable = 10
    declared = str(10 + readable)
    [outcome] = ingest_file(write(tmp_path, "rec-1", [good, bad], declared_packs=declared))
    assert outcome.status == "published", outcome.as_dict()
    assert outcome.applied == 1 and outcome.quarantine_reasons == {reason: 1}
    [row] = q("SELECT reason, payload FROM app_ingest.quarantine WHERE batch_id = 'rec-1'")
    assert row["reason"] == reason and isinstance(row["payload"], dict)
    assert non_finite_published() == 0


def test_a_record_that_is_not_an_object_or_repeats_a_key_is_malformed(fresh, per_record,
                                                                       tmp_path):
    text = ('{"source_system": "test-feed", "batch_id": "rec-shape", "declared_count": 3, '
            '"declared_pack_units": "20", "events": [' + VALID + ', "just text", '
            '{"source_event_id": "dup", "source_event_id": "dup", "event_version": 1, '
            '"kind": "upsert", "event_time": "2026-09-18T10:00:00-04:00", "org_id": "FA001", '
            '"ndc": "11111-0101-01", "data_source": "distributor", "pack_units": 10, '
            '"unit": "packs", "wac": 1200.0}]}')
    [outcome] = ingest_file(raw_file(tmp_path, "rec-shape", text.encode()))
    assert outcome.status == "published" and outcome.applied == 1
    assert outcome.quarantine_reasons == {"malformed_record": 2}


# -- reconciliation and the quality threshold count what could not be read ------------

def test_control_totals_count_records_that_could_not_be_read(fresh, per_record, tmp_path):
    """The source's totals cover everything it sent. A total that leaves the
    malformed record out does not reconcile."""
    [outcome] = ingest_file(write(tmp_path, "rec-totals", [event("t-ok"),
                                                           event("t-bad", wac="@NaN@")],
                                  declared_count=1, declared_packs="10"))
    assert (outcome.status, outcome.rejection_code) == ("rejected", "control_totals")


def test_malformed_records_count_toward_the_quality_threshold(fresh, tmp_path):
    before = snapshot()
    [outcome] = ingest_file(write(tmp_path, "rec-quality", [event("qa-ok"),
                                                            event("qa-bad", version=1.5)]))
    assert (outcome.status, outcome.rejection_code) == ("rejected", "quality_threshold")
    assert snapshot() == before


# -- corrections, deletions, duplicates and replay, mixed with bad records ------------

def test_corrections_deletions_and_duplicates_still_apply_beside_bad_records(fresh, per_record,
                                                                              tmp_path):
    [first] = ingest_file(write(tmp_path, "mix-1", [event(f"mx-{i}") for i in range(3)]))
    assert first.status == "published" and first.applied == 3
    deletion = {"source_event_id": "mx-1", "event_version": 2, "kind": "delete",
                "event_time": "2026-09-25T09:00:00-04:00"}
    [second] = ingest_file(write(tmp_path, "mix-2", [
        event("mx-0", version=2, packs=12),            # a correction
        deletion,                                       # a deletion
        event("mx-2"),                                  # a duplicate of what is applied
        event("mx-bad-1", wac="@NaN@"),
        event("mx-bad-2", version="@true@"),
    ], declared_packs="42"))   # 12 + 10 + 10 + 10: the bad records' quantities count too
    assert second.status == "published"
    assert (second.corrected, second.tombstoned, second.duplicates) == (1, 1, 1)
    assert second.quarantine_reasons == {"non_finite_number": 1, "invalid_identity": 1}
    packs = {r["source_event_id"]: r for r in q(
        "SELECT l.source_event_id, l.event_version, l.tombstoned, s.pack_units "
        "FROM app_ingest.event_ledger l LEFT JOIN sales s ON s.sale_id = l.sale_id "
        "WHERE l.source_event_id LIKE 'mx-%%'")}
    assert packs["mx-0"]["event_version"] == 2 and packs["mx-0"]["pack_units"] == 12
    assert packs["mx-1"]["tombstoned"] and packs["mx-1"]["pack_units"] is None
    assert "mx-bad-1" not in packs and "mx-bad-2" not in packs


def test_a_rejected_batch_can_be_corrected_and_resent_under_its_id(fresh, tmp_path):
    before = snapshot()
    [rejected] = ingest_file(write(tmp_path, "resend-1", [event("rs-1")],
                                   declared_packs="@NaN@"))
    assert rejected.rejection_code == "invalid_envelope" and snapshot() == before
    [accepted] = ingest_file(write(tmp_path, "resend-1", [event("rs-1")]))
    assert accepted.status == "published" and accepted.attempt == 2
    [replayed] = ingest_file(write(tmp_path, "resend-1", [event("rs-1")]))
    assert replayed.status == "no_change" and replayed.duplicates == 1
    [row] = q("SELECT status, attempts, rejection_code FROM app_ingest.batches "
              "WHERE batch_id = 'resend-1'")
    assert (row["status"], row["attempts"], row["rejection_code"]) == ("no_change", 3, None)


def test_a_quarantined_record_fixed_and_resent_is_applied_once(fresh, per_record, tmp_path):
    [first] = ingest_file(write(tmp_path, "fix-1", [event("fx-ok"),
                                                    event("fx-late", wac="@NaN@")]))
    assert first.applied == 1 and first.quarantined == 1
    [fixed] = ingest_file(write(tmp_path, "fix-2", [event("fx-ok"), event("fx-late")]))
    assert (fixed.applied, fixed.duplicates, fixed.quarantined) == (1, 1, 0)
    assert q("SELECT count(*) AS n FROM app_ingest.event_ledger "
             "WHERE source_event_id LIKE 'fx-%%'")[0]["n"] == 2


# -- the parser changes nothing for a valid event ---------------------------------------

def test_a_valid_event_digests_exactly_as_the_old_reader_made_it():
    """Replays of batches ingested before the strict reader must still be
    duplicates, so a valid event must come out with the same values -- and
    the same digest -- as json.loads and SourceEvent(**raw) produced."""
    from datetime import datetime

    from app.data.ingest import _digest
    from app.data.sources import parse_event

    for raw in (event("d-1"), event("d-2", packs=10.5, wac=99.99),
                event("d-3", packs=7, wac=1e3), event("d-4", version=3, wac=0.1),
                {"source_event_id": "d-5", "event_version": 2, "kind": "delete",
                 "event_time": "2026-09-25T09:00:00-04:00"}):
        text = json.dumps(raw)
        old = dict(json.loads(text))
        old["event_time"] = datetime.fromisoformat(old["event_time"]) if old.get("event_time") \
            else None
        new = parse_event(json.loads(text, parse_float=__import__("decimal").Decimal))
        assert _digest(new) == _digest(SourceEvent(**old)), raw


@pytest.mark.parametrize('field,value', [('declared_count', 2**31),
    ('declared_pack_units', '1e999999'), ('declared_pack_units', '1e-999999')])
def test_unstorable_envelope_is_rejected_and_recorded(fresh, field, value):
    from dataclasses import replace
    from tests.integration.test_ingestion import batch, ev, run, state
    outcome = run(replace(batch('bounds', ev('bounds')), **{field: value}))
    assert outcome.status == 'rejected'
    assert outcome.rejection_code == 'invalid_envelope'
    assert state() == fresh
    assert q("SELECT status FROM app_ingest.batches WHERE batch_id='bounds'")[0]['status'] == 'rejected'


def test_mixed_decimal_huge_integer_and_surrogate_batch_is_atomic_and_replayable(fresh, per_record):
    from decimal import Decimal
    from tests.integration.test_ingestion import batch, ev, run, state
    b = batch('edge-mixed', ev('decimal', packs=Decimal('10.5')),
              ev('huge', packs=10**400), ev('surrogate\ud800'))
    first = run(b)
    assert first.status == 'published'
    assert (first.applied, first.quarantined) == (1, 2)
    after = state()
    assert after['rows'] == fresh['rows'] + 1
    assert after['packs'] == fresh['packs'] + Decimal('10.5')
    second = run(b)
    assert second.duplicates == 1 and second.applied == 0
    assert state() == after
    assert len(q("SELECT payload FROM app_ingest.quarantine WHERE batch_id='edge-mixed'")) == 4
