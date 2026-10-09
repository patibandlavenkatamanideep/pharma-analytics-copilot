"""One request, one generation of data.

Review finding 6. A request read the manifest, the vocabulary and the facts
in separate transactions; a refresh between them could plan against one
generation's calendar and vocabulary and execute against another's rows.

The fix has three parts, each tested here against the disposable database:

1. execution reads the published generation and the facts under ONE
   repeatable-read snapshot, and does not run if the generation is not the
   one the request planned against -- the request ends with an explicit
   `refresh`, and a retry with the same key answers on the new data;
2. publication is MVCC-safe, so a snapshot taken before a refresh keeps
   seeing the old generation whole -- marker and facts together;
3. publication does not block readers. Measured both ways: an uncommitted
   DELETE-based publication leaves a reader answering in milliseconds; an
   uncommitted TRUNCATE, which the loader used to do, holds the reader until
   its statement timeout.
"""

from __future__ import annotations

import secrets
import threading
import time

import psycopg
import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security


def owner_connection():
    from app.config import get_settings
    from psycopg.rows import dict_row
    return psycopg.connect(get_settings().dsn("owner"), row_factory=dict_row)


def current_generation():
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT dataset_id FROM app_ref.generation")
        return cur.fetchone()["dataset_id"]


def set_generation(dataset_id):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("UPDATE app_ref.generation SET dataset_id = %s", (dataset_id,))


def test_a_refresh_between_planning_and_execution_is_not_answered_with_mixed_data(
        client, make_identity, monkeypatch):
    from app.pipeline import Turn

    original = Turn.node_plan
    real = current_generation()

    def plan_then_refresh(self, state):
        out = original(self, state)
        set_generation("refreshed-" + secrets.token_hex(3))   # a publish lands now
        return out

    monkeypatch.setattr(Turn, "node_plan", plan_then_refresh)
    sign_in(client, make_identity("exec", can_view_wac=1))
    key = "k-" + secrets.token_hex(8)
    try:
        body = client.post("/api/ask", json={"question": "What is our total volume this quarter?"},
                           headers={"Idempotency-Key": key}).json()
        assert body["status"] == "refresh", body
        assert "answer" not in body
        assert body["persistence"] == "not_saved"
    finally:
        set_generation(real)
    monkeypatch.setattr(Turn, "node_plan", original)

    # The run was closed as failed, so the same key runs again -- and the
    # data now agrees with what the request reads at its start.
    retried = client.post("/api/ask", json={"question": "What is our total volume this quarter?"},
                          headers={"Idempotency-Key": key}).json()
    assert retried["status"] == "answered", retried


def test_an_older_snapshot_keeps_seeing_its_generation_whole(authtest_db):
    """The marker and the facts move together, and an older snapshot sees
    neither move."""
    from app.db import analytics_transaction

    real = current_generation()
    with analytics_transaction(scope_kind="global", scope_value=None, wac_authorized=False,
                               expect_generation=real) as reader:
        reader.execute("SELECT count(*) AS n FROM sales")
        before = reader.fetchone()["n"]

        # A publication commits while the reader's snapshot is open.
        with owner_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT sale_id FROM sales LIMIT 1")
            victim = cur.fetchone()["sale_id"]
            cur.execute("CREATE TEMP TABLE keep AS SELECT * FROM sales WHERE sale_id = %s",
                        (victim,))
            cur.execute("DELETE FROM sales WHERE sale_id = %s", (victim,))
            cur.execute("UPDATE app_ref.generation SET dataset_id = 'next-generation'")
            conn.commit()
            try:
                reader.execute("SELECT dataset_id FROM app_ref.generation")
                assert reader.fetchone()["dataset_id"] == real, "the marker moved under the snapshot"
                reader.execute("SELECT count(*) AS n FROM sales")
                assert reader.fetchone()["n"] == before, "the facts moved under the snapshot"
            finally:
                cur.execute("INSERT INTO sales SELECT * FROM keep")
                cur.execute("UPDATE app_ref.generation SET dataset_id = %s", (real,))
                conn.commit()


def test_a_query_refuses_to_run_on_a_generation_it_did_not_plan_against(authtest_db):
    from app.db import GenerationChanged, analytics_transaction

    with pytest.raises(GenerationChanged):
        with analytics_transaction(scope_kind="global", scope_value=None, wac_authorized=False,
                                   expect_generation="a-generation-that-is-not-published"):
            pass


def _reader_latency(seconds_budget: float) -> tuple[float, Exception | None]:
    """Run one analytics query with a short statement timeout; return how
    long it took and any error."""
    from app.config import get_settings
    from app.db import analytics_transaction

    settings = get_settings().model_copy(update={"statement_timeout_ms": int(seconds_budget * 1000)})
    started = time.perf_counter()
    try:
        with analytics_transaction(scope_kind="global", scope_value=None, wac_authorized=False,
                                   settings=settings) as cur:
            cur.execute("SELECT count(*) AS n FROM sales")
            cur.fetchone()
        return time.perf_counter() - started, None
    except Exception as exc:
        return time.perf_counter() - started, exc


@pytest.mark.parametrize("statement,blocks", [
    ("DELETE FROM sales", False),        # what publication does now
    ("TRUNCATE sales CASCADE", True),    # what it used to do
])
def test_publication_does_not_block_readers(authtest_db, statement, blocks):
    holding = threading.Event()
    release = threading.Event()

    def publisher():
        with owner_connection() as conn, conn.cursor() as cur:
            cur.execute(statement)       # uncommitted: a publication in progress
            holding.set()
            release.wait(10)
            conn.rollback()

    thread = threading.Thread(target=publisher)
    thread.start()
    assert holding.wait(10)
    try:
        elapsed, error = _reader_latency(1.5)
    finally:
        release.set()
        thread.join(10)

    if blocks:
        assert error is not None and elapsed >= 1.4, "TRUNCATE did not block -- check the premise"
    else:
        assert error is None, f"a DELETE-based publication blocked a reader: {error}"
        assert elapsed < 1.0, f"reader took {elapsed:.2f}s during publication"
