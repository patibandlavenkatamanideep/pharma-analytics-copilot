"""A snapshot and the manifest that describes it must become visible together.

The manifest is not metadata. It carries the reporting anchor, which is how
"this quarter" is resolved. Publishing it in a second transaction left a window
where the new rows were live under the previous snapshot's calendar, with
dataset_id, row counts and coverage all describing data that no longer existed.
A crash in that window left the rows loaded and nothing published at all.

Runs against the disposable database: it truncates and reloads business tables.
"""

from __future__ import annotations

import os

import pytest

AUTHTEST_DB = os.environ.get("PAC_AUTHTEST_DB", "pharma_analytics_authtest")


@pytest.fixture(scope="module")
def loadable_db():
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools

    if AUTHTEST_DB == "pharma_analytics":
        pytest.fail("refusing to run load tests against the working database")

    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = AUTHTEST_DB
    get_settings.cache_clear()
    close_pools()
    clear_caches()
    try:
        from app.db import owner_transaction

        with owner_transaction() as cur:
            cur.execute("SELECT 1 FROM app_meta.dataset_manifest LIMIT 1")
        yield
    except Exception as exc:
        pytest.skip(f"authtest database unavailable: {type(exc).__name__}: {exc}")
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


def published(cur):
    cur.execute(
        "SELECT dataset_id, row_counts, reporting_anchor FROM app_meta.dataset_manifest "
        "WHERE load_state = 'published' ORDER BY published_at DESC"
    )
    return cur.fetchall()


pytestmark = pytest.mark.integration


def test_a_reload_succeeds_and_can_be_repeated(loadable_db):
    """Any reload used to die on users_pkey before touching a business table.

    The seed file inserts users; business tables are truncated on reload and
    `users` deliberately is not, because credentials in app_auth are keyed to
    it. So the second load -- and in practice the first, on any database that
    already had users -- failed outright.
    """
    from app.data.loader import load

    ids = [load("seed").dataset_id for _ in range(3)]
    assert len(set(ids)) == 3, f"dataset ids were reused across loads: {ids}"


def test_exactly_one_snapshot_is_published_after_a_reload(loadable_db):
    from app.data.loader import load
    from app.db import owner_transaction

    load("seed")
    with owner_transaction() as cur:
        rows = published(cur)
    assert len(rows) == 1, f"{len(rows)} published snapshots"


def test_the_published_manifest_describes_the_rows_that_are_live(loadable_db):
    """The counts and the anchor must be the ones for THIS data, not the last."""
    from app.data.loader import load
    from app.db import owner_transaction

    report = load("seed")
    with owner_transaction() as cur:
        row = published(cur)[0]
        cur.execute("SELECT count(*) AS n FROM sales")
        actual_sales = cur.fetchone()["n"]

    assert row["dataset_id"] == report.dataset_id
    assert row["row_counts"]["sales"] == actual_sales
    assert row["reporting_anchor"] == report.reporting_anchor


def test_a_failed_load_leaves_the_previous_snapshot_serving(loadable_db):
    """Failing closed means the old answer keeps working, not that none does."""
    from app.data import loader
    from app.db import owner_transaction

    loader.load("seed")
    with owner_transaction() as cur:
        before = published(cur)[0]["dataset_id"]

    original = loader._validate

    def explode(cur, report):
        raise RuntimeError("injected validation failure")

    loader._validate = explode
    try:
        with pytest.raises(RuntimeError, match="injected"):
            loader.load("seed")
    finally:
        loader._validate = original

    with owner_transaction() as cur:
        rows = published(cur)
        cur.execute("SELECT count(*) AS n FROM sales")
        assert cur.fetchone()["n"] > 0, "a failed load left the tables empty"
    assert len(rows) == 1
    assert rows[0]["dataset_id"] == before, "a failed load superseded the good snapshot"


def test_the_vocabulary_follows_the_published_snapshot(loadable_db):
    """A reload must not leave entity names cached from the previous one."""
    from app.analytics.entities import _product_vocabulary, _current_dataset_id
    from app.data.loader import load

    load("seed")
    first_id = _current_dataset_id()
    first = _product_vocabulary(first_id)

    load("seed")
    second_id = _current_dataset_id()
    assert second_id != first_id, "the dataset id did not change across a reload"
    # A different key, so the cache cannot serve the old snapshot's answer.
    assert _product_vocabulary(second_id) is not first
