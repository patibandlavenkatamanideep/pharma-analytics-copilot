"""Charts carry the authorized answer's units, grain and snapshot freshness."""
import secrets

import pytest

from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]


def test_chart_metadata_survives_replay_without_using_newer_freshness(pipeline, ram_user, monkeypatch):
    from app.pipeline import to_payload

    key = "chart-" + secrets.token_hex(8)
    question = "Show monthly paid pack units over the last 6 months"
    first = pipeline.ask(ram_user, question, idempotency_key=key)
    assert first.status == "answered"
    original = to_payload(first, False)["answer"]
    assert original["unit"] == "packs"
    assert original["dimensions"] == ["period_mo"]
    assert original["data_through"]
    assert original["rows"] == first.answer.table
    dataset = pipeline.current_dataset()
    changed = {**dataset, "reporting_anchor": {**dataset["reporting_anchor"], "max_txn": "2099-01-01"}}
    monkeypatch.setattr(pipeline, "current_dataset", lambda: changed)
    replay = pipeline.ask(ram_user, question, idempotency_key=key)
    assert replay.payload["replayed"]
    assert replay.payload["answer"] == original
