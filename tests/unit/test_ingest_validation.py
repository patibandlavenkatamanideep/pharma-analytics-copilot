"""Event validation for adapters that build SourceEvents themselves.

The JSON reader (app/data/sources.parse_batch) checks types where data
arrives; app/data/ingest._field_problem checks them again for any other
adapter, because a dataclass does not enforce its annotations. Review of
1 October 2026, R5: NaN and infinite values passed, and a string quantity
raised TypeError instead of producing a reason.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.data.ingest import _field_problem
from app.data.sources import SourceEvent, readable_packs, storable

ORG, NDC = "FA001", "11111-0101-01"
WHEN = datetime(2026, 9, 18, 14, tzinfo=timezone.utc)


def event(**overrides) -> SourceEvent:
    base = dict(source_event_id="v-1", event_version=1, kind="upsert", event_time=WHEN,
                org_id=ORG, ndc=NDC, data_source="distributor", pack_units=10.0, wac=1200.0)
    return SourceEvent(**{**base, **overrides})


def problem(e: SourceEvent):
    return _field_problem(e, {ORG}, {NDC}, date(2026, 10, 10), ZoneInfo("America/New_York"))


@pytest.mark.parametrize("overrides,reason", [
    ({}, None),
    ({"pack_units": 10}, None),
    ({"pack_units": Decimal("10.5"), "wac": Decimal("1200")}, None),
    ({"wac": math.nan}, "non_finite_number"),
    ({"wac": math.inf}, "non_finite_number"),
    ({"wac": -math.inf}, "non_finite_number"),
    ({"pack_units": math.inf}, "non_finite_number"),
    ({"pack_units": math.nan}, "non_finite_number"),
    ({"pack_units": "10"}, "invalid_type"),
    ({"pack_units": True}, "invalid_type"),
    ({"wac": "1200"}, "invalid_type"),
    ({"wac": False}, "invalid_type"),
    ({"org_id": 7}, "invalid_type"),
    ({"event_version": True}, "invalid_identity"),
    ({"event_version": 1.5}, "invalid_identity"),
    ({"event_version": "1"}, "invalid_identity"),
    ({"event_version": 0}, "invalid_identity"),
    ({"source_event_id": 5}, "invalid_identity"),
    ({"source_event_id": "a\x00b"}, "invalid_identity"),
    ({"event_time": "2026-09-18T10:00:00-04:00"}, "invalid_timestamp"),
    ({"pack_units": 2_000_000}, "out_of_range"),
    ({"wac": 2e7}, "out_of_range"),
    ({"pack_units": -1}, "non_positive_packs"),
    ({"wac": -1.0}, "invalid_price"),
])
def test_every_problem_is_a_reason_never_an_exception(overrides, reason):
    assert problem(event(**overrides)) == reason


@pytest.mark.parametrize("value,expected", [
    (10, Decimal(10)), ("10.5", Decimal("10.5")), (Decimal("7"), Decimal(7)),
    (math.nan, Decimal(0)), (math.inf, Decimal(0)), ("Infinity", Decimal(0)),
    (Decimal("1E+309"), Decimal(0)), (True, Decimal(0)), ("ten", Decimal(0)), (None, Decimal(0)),
    ([1], Decimal(0)),
])
def test_reconciliation_reads_only_storable_quantities(value, expected):
    assert readable_packs(value) == expected


def test_anything_quarantined_can_be_stored_as_jsonb():
    """No NaN token, no NUL, no lone surrogate, bounded size and depth."""
    import json
    nested: list = []
    deep = nested
    for _ in range(20):
        deep.append([])
        deep = deep[0]
    payload = {"wac": math.nan, "packs": math.inf, "big": Decimal("1E+309"),
               "id": "a\x00b\ud800c", "huge": 10**30, "nested": nested,
               "long": "x" * 5_000, "when": WHEN, "obj": object()}
    text = json.dumps(storable(payload), allow_nan=False)
    assert "\\u0000" not in text and "NaN" in text and len(text) < 3_000
    assert "\\ud800" not in text


# -- the JSON reader classifies on its own, before ingestion sees an event ------------

def read(fields: str):
    """One record, as JSON text, through the strict reader."""
    from app.data.sources import _load, parse_event
    base = ('"source_event_id": "p-1", "event_version": 1, "kind": "upsert", '
            '"event_time": "2026-09-18T10:00:00-04:00", "org_id": "FA001", '
            '"ndc": "11111-0101-01", "data_source": "distributor", "unit": "packs"')
    return parse_event(_load("{" + base + ", " + fields + "}"))


@pytest.mark.parametrize("fields,reason", [
    ('"pack_units": 10, "wac": NaN', "non_finite_number"),
    ('"pack_units": Infinity, "wac": 1.0', "non_finite_number"),
    ('"pack_units": 1e309, "wac": 1.0', "non_finite_number"),
    ('"pack_units": 10, "wac": -1e400', "non_finite_number"),
    ('"pack_units": "10", "wac": 1.0', "invalid_type"),
    ('"pack_units": true, "wac": 1.0', "invalid_type"),
    ('"pack_units": [10], "wac": 1.0', "invalid_type"),
    ('"pack_units": 10, "wac": 1.0, "rebate": 2', "unexpected_field"),
])
def test_the_reader_classifies_values_itself(fields, reason):
    from app.data.sources import Malformed
    record = read(fields)
    assert isinstance(record, Malformed) and record.reason == reason


def test_the_reader_keeps_the_values_json_would_have_made():
    from app.data.sources import SourceEvent
    record = read('"pack_units": 10, "wac": 1200.0')
    assert isinstance(record, SourceEvent)
    assert record.pack_units == 10 and type(record.pack_units) is int
    assert record.wac == 1200.0 and type(record.wac) is float
