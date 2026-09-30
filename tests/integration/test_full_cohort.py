"""A 908-account answer, followed by "those same accounts", keeps all 908.

Review finding 4: a 500-account answer retained 200 ids, and the plan's
account filter holds only 200, so "those same accounts" silently became a
smaller population. The cohort is now stored whole and bound to the query by
the server.

On the full dataset, Mid-Atlantic has 908 accounts with paid sales in the
last three months. Every expectation here is computed by SQL written in this
file.
"""

from __future__ import annotations

import pytest

from tests.conftest import needs_full

pytestmark = needs_full

REGION = "Mid-Atlantic"


@pytest.fixture(scope="module")
def truth():
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        cur.execute("""
            SELECT COALESCE(o.grandparent_org_id, o.org_id) AS account,
                   sum(s.pack_units) AS v
            FROM sales s JOIN organizations o ON o.org_id = s.org_id
            JOIN zip_territory z ON z.zip = o.zip
            WHERE s.data_source = 'distributor' AND s.brand_flag = 1
              AND s.mo_offset IN (0, 1, 2) AND z.region_name = %s
            GROUP BY 1""", (REGION,))
        rows = cur.fetchall()
    return {r["account"]: float(r["v"]) for r in rows}


@pytest.fixture(scope="module")
def conversation(pipeline, exec_user):
    first = pipeline.ask(
        exec_user, f"Show paid pack units by account in the {REGION} region for the last 3 months")
    assert first.status == "answered", first.message
    follow = pipeline.ask(exec_user, "Show me those same accounts by month",
                          conversation_id=first.conversation_id)
    return first, follow


def test_the_first_answer_covers_every_account(conversation, truth):
    first, _ = conversation
    assert len(truth) > 200, "the fixture must exceed the old 200-id cap to mean anything"
    assert first.answer.row_count == len(truth)
    assert not first.answer.truncated


def test_the_follow_up_applies_all_of_them(conversation, truth):
    _, follow = conversation
    assert follow.status == "answered", follow.message
    dimension, ids = follow.applied_cohort
    assert dimension == "account"
    assert set(ids) == set(truth), (
        f"{len(ids)} accounts applied, {len(truth)} in the previous answer")


def test_the_follow_up_totals_are_the_same_population(conversation, truth):
    """Metamorphic: regrouping the same accounts over the same months moves
    no volume. A cohort cut to 200 would lose most of it."""
    _, follow = conversation
    total = sum(float(r["value"] or 0) for r in follow.answer.table)
    assert total == pytest.approx(sum(truth.values()))


def test_the_stored_cohort_is_whole_and_distinct(conversation, truth):
    from app.db import owner_transaction
    first, _ = conversation
    with owner_transaction() as cur:
        cur.execute("""
            SELECT c.member_count, c.complete, count(m.entity_id) AS stored,
                   count(DISTINCT m.entity_id) AS distinct_stored
            FROM app_conv.turns t JOIN app_conv.cohorts c ON c.cohort_id = t.cohort_id
            JOIN app_conv.cohort_members m ON m.cohort_id = c.cohort_id
            WHERE t.conversation_id = %s AND t.seq = 1
            GROUP BY c.member_count, c.complete""", (first.conversation_id,))
        row = cur.fetchone()
    assert row["member_count"] == row["stored"] == row["distinct_stored"] == len(truth)
    assert row["complete"] is True
