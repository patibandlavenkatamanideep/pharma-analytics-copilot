"""A bound of exactly N, against real accounts that sit exactly on it.

The unit tests prove each operator compiles to the right comparison. This
proves the thing the review actually observed: that an account with exactly
the bound value is in or out of the ANSWER as the question requires.

The reference values come from SQL written here, not from the compiler under
test.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan
from tests.conftest import needs_db

pytestmark = needs_db

R3M = {"kind": "named", "named": "r3m"}


@pytest.fixture(scope="module")
def boundary():
    """A pack-unit total that several accounts hit exactly in r3m, with the
    accounts that hit it -- computed independently of the compiler."""
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""
            WITH totals AS (
                SELECT COALESCE(o.grandparent_org_id, o.org_id) AS account,
                       sum(s.pack_units) AS v
                FROM sales s JOIN organizations o ON o.org_id = s.org_id
                WHERE s.data_source = 'distributor' AND s.brand_flag = 1
                  AND s.mo_offset IN (0, 1, 2)
                GROUP BY 1)
            SELECT v, array_agg(account ORDER BY account) AS accounts,
                   (SELECT count(*) FROM totals t2 WHERE t2.v > t.v)  AS n_gt,
                   (SELECT count(*) FROM totals t2 WHERE t2.v < t.v)  AS n_lt
            FROM totals t
            WHERE v BETWEEN 50 AND 5000
            GROUP BY v HAVING count(*) >= 2
            ORDER BY count(*) DESC, v LIMIT 1""")
        row = cur.fetchone()
    assert row, "no pack total is shared by two accounts; pick another fixture"
    return float(row["v"]), set(row["accounts"]), int(row["n_gt"]), int(row["n_lt"])


def accounts(anchor, op, value):
    from app.db import owner_transaction
    plan = AnalyticalPlan.model_validate({
        "metric": "paid_pack_units", "dimensions": ["account"], "time": R3M,
        "threshold": {"op": op, "value": value}})
    q = Compiler(max_rows=100_000).compile(plan, anchor=anchor)
    with owner_transaction() as cur:
        cur.execute(q.sql, q.params)
        return {r["dim0_id"] for r in cur.fetchall()}


def test_at_least_includes_the_accounts_exactly_at_the_bound(anchor, boundary):
    value, at_bound, n_gt, _ = boundary
    got = accounts(anchor, "gte", value)
    assert at_bound <= got, "an account exactly at the bound was dropped"
    assert len(got) == n_gt + len(at_bound)


def test_more_than_excludes_them(anchor, boundary):
    value, at_bound, n_gt, _ = boundary
    got = accounts(anchor, "gt", value)
    assert not (at_bound & got)
    assert len(got) == n_gt


def test_at_most_includes_them(anchor, boundary):
    value, at_bound, _, n_lt = boundary
    got = accounts(anchor, "lte", value)
    assert at_bound <= got
    assert len(got) == n_lt + len(at_bound)


def test_less_than_excludes_them(anchor, boundary):
    value, at_bound, _, n_lt = boundary
    got = accounts(anchor, "lt", value)
    assert not (at_bound & got)
    assert len(got) == n_lt


def test_the_two_sides_partition_every_account(anchor, boundary):
    """Metamorphic: 'at least N' and 'less than N' are complements."""
    value, *_ = boundary
    upper, lower = accounts(anchor, "gte", value), accounts(anchor, "lt", value)
    assert not upper & lower
    everything = accounts(anchor, "gte", float("-1e18"))
    assert upper | lower == everything
