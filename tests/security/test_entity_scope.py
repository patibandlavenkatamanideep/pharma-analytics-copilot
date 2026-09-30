"""Named accounts on the real request path, under real scopes.

An account the caller can see is resolved to its id and the answer is that
account's figure -- checked against SQL written here. An account the caller
cannot see is refused in exactly the words used for an account that does
not exist, so the refusal cannot be used to discover what exists elsewhere.
"""

from __future__ import annotations

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security

QUESTION = "What was the volume for {} in the last 3 months?"


def territory_and_region_of(account_id):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""
            SELECT min(z.territory_name) AS t, min(z.region_name) AS r
            FROM organizations o JOIN zip_territory z ON z.zip = o.zip
            WHERE COALESCE(o.grandparent_org_id, o.org_id) = %s""", (account_id,))
        row = cur.fetchone()
    return row["t"], row["r"]


def other_territory(than):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""SELECT territory_name AS t, region_name AS r FROM zip_territory
                       WHERE territory_name <> %s ORDER BY 1 LIMIT 1""", (than,))
        row = cur.fetchone()
    return row["t"], row["r"]


def ask(client, question):
    r = client.post("/api/ask", json={"question": question, "include_sql": True})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def memorial(authtest_db):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""SELECT COALESCE(grandparent_org_id, org_id) AS id,
                              COALESCE(grandparent_org_name, org_name) AS label
                       FROM organizations
                       WHERE COALESCE(grandparent_org_name, org_name) = 'Memorial Health System'
                       LIMIT 1""")
        row = cur.fetchone()
    if row is None:
        pytest.fail("authtest data no longer has Memorial Health System; pick another account")
    return row["id"], row["label"]


def test_a_named_account_in_scope_is_answered_for_that_account(client, make_identity, memorial):
    from app.db import owner_transaction
    account_id, label = memorial
    territory, region = territory_and_region_of(account_id)
    sign_in(client, make_identity("ram", territory=territory, region=region))

    body = ask(client, QUESTION.format(label.lower()))      # casing must not matter

    assert body["status"] == "answered", body
    assert body["plan"]["filters"]["account_ids"] == [account_id]
    with owner_transaction() as cur:
        cur.execute("""
            SELECT COALESCE(sum(s.pack_units), 0) AS v
            FROM sales s JOIN organizations o ON o.org_id = s.org_id
            WHERE COALESCE(o.grandparent_org_id, o.org_id) = %s
              AND s.data_source = 'distributor' AND s.brand_flag = 1
              AND s.mo_offset IN (0, 1, 2)""", (account_id,))
        want = float(cur.fetchone()["v"])
    got = float(body["answer"]["rows"][0]["value"]) if body["answer"]["rows"] else 0.0
    assert got == pytest.approx(want)


def test_an_account_outside_scope_is_refused_like_one_that_does_not_exist(
        client, make_identity, memorial):
    account_id, label = memorial
    territory, region = other_territory(territory_and_region_of(account_id)[0])
    sign_in(client, make_identity("ram", territory=territory, region=region))

    real = ask(client, QUESTION.format(label))
    fake = ask(client, QUESTION.format("Northwind Regional Health"))

    assert real["status"] == fake["status"] == "clarify"
    # Same template, the name swapped: nothing distinguishes "exists
    # elsewhere" from "exists nowhere".
    assert real["message"].replace(label, "X") == fake["message"].replace(
        "Northwind Regional Health", "X")
    assert account_id not in str(real)


def test_an_unknown_product_in_lower_case_is_refused_not_answered(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    body = ask(client, "What is the volume for floobertax this quarter?")
    assert body["status"] == "clarify"
    assert "floobertax" in body["message"].lower()
    assert "answer" not in body
