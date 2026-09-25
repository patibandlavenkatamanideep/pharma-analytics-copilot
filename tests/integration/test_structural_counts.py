"""A facility does not stop existing because it had no sales last quarter.

facility_count is derived from the sales table, so it answers "how many
facilities did we sell to". Asked "how many facilities does this health system
have", that silently excluded 14,439 of 40,000 facilities and said nothing.

facility_count_all reads the organization hierarchy directly. Row-level
security still applies, because organizations carries the same policy as
sales -- a structural count is not a way around scope.
"""

from __future__ import annotations

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan
from app.analytics.validator import validate
from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]


def run(metric, anchor, *, scope_kind="global", scope_value=None):
    from app.db import analytics_transaction

    plan = AnalyticalPlan.model_validate(
        {"metric": metric, "time": {"kind": "named", "named": "r3m"}})
    query = Compiler().compile(plan, anchor=anchor)
    validate(query.sql, wac_authorized=False)
    with analytics_transaction(scope_kind=scope_kind, scope_value=scope_value,
                               wac_authorized=False) as cur:
        cur.execute(query.sql, query.params)
        return cur.fetchone()["value"], query


def oracle(sql, *, scope_kind="global", scope_value=None):
    from app.db import analytics_transaction

    with analytics_transaction(scope_kind=scope_kind, scope_value=scope_value,
                               wac_authorized=False) as cur:
        cur.execute(sql)
        return list(cur.fetchone().values())[0]


def test_the_structural_count_counts_facilities_not_every_organization(anchor):
    """org_hierarchy.md defines a facility as org_type = 'Facility'.

    organizations holds all three levels -- 37,500 facilities, 2,000 parents
    and 500 grandparents -- so counting every row reported 40,000 facilities,
    2,500 of which are not facilities. The sales-derived count never hit this
    because sales.org_id is always a facility.
    """
    value, _ = run("facility_count_all", anchor)
    assert value == oracle(
        "SELECT count(DISTINCT org_id) FROM organizations WHERE org_type = 'Facility'")
    assert value < oracle("SELECT count(DISTINCT org_id) FROM organizations")


def test_the_sales_derived_count_is_strictly_smaller(anchor):
    """If these agreed, the distinction would be pointless."""
    structural, _ = run("facility_count_all", anchor)
    with_sales, _ = run("facility_count", anchor)
    assert with_sales < structural
    assert with_sales == oracle(
        "SELECT count(DISTINCT o.org_id) FROM organizations o "
        "JOIN sales s ON s.org_id = o.org_id "
        "WHERE s.data_source = 'distributor' AND s.brand_flag = 1 "
        "AND s.mo_offset IN (0,1,2)")


def test_a_structural_count_is_still_bounded_by_row_level_security(anchor):
    """The point of reading organizations directly is to escape the SALES
    join, not to escape the scope boundary."""
    from app.db import owner_transaction

    with owner_transaction() as cur:
        cur.execute("SELECT territory_name FROM zip_territory "
                    "WHERE territory_name IS NOT NULL LIMIT 1")
        territory = cur.fetchone()["territory_name"]

    scoped, _ = run("facility_count_all", anchor,
                    scope_kind="territory", scope_value=territory)
    everything, _ = run("facility_count_all", anchor)
    assert 0 < scoped < everything, (
        f"a territory count of {scoped} against {everything} company-wide"
    )
    assert scoped == oracle(
        "SELECT count(DISTINCT org_id) FROM organizations WHERE org_type = 'Facility'",
        scope_kind="territory", scope_value=territory)


def test_the_structural_count_does_not_read_the_sales_table(anchor):
    _, query = run("facility_count_all", anchor)
    assert "FROM organizations" in query.sql
    assert " sales " not in query.sql and "FROM sales" not in query.sql


def test_the_answer_says_it_is_not_time_bounded(anchor):
    """Otherwise "40,000 facilities" reads as "this quarter"."""
    _, query = run("facility_count_all", anchor)
    assert "structural" in query.window_label.lower()
    assert any("whether or not they had sales" in n for n in query.notes), query.notes


def test_the_sales_derived_count_is_still_time_bounded(anchor):
    _, query = run("facility_count", anchor)
    assert "mo_offset" in query.sql
