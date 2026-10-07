"""k-07 end to end: the supplied sample question, asked of the real pipeline on the
release dataset, against an independent oracle.

"Is Zenovax volume growing or declining month over month?" (docs/product_analytics.md)
is answered with each month's paid pack units, the month before, the change and the
percentage, and a headline that says which way the latest month moved. The oracle is
SQL written here over the reporting calendar, with the lag taken in Python: nothing
from the compiler. A territory-scoped user gets the same shape over their own
territory only, and their territory has data, so the check is not vacuous.
"""

from __future__ import annotations

import pytest

from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]

QUESTION = "Is Zenovax volume growing or declining month over month?"


def oracle(territory: str | None) -> dict[str, dict[str, float | None]]:
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT period_mo, min(wk_offset) AS newest, "
                    "bool_and('distributor' = ANY(sources)) AS covered "
                    "FROM app_ref.calendar GROUP BY period_mo ORDER BY newest DESC")
        months = [(r["period_mo"], r["covered"]) for r in cur.fetchall()]
        where = ("s.data_source = 'distributor' AND s.brand_flag = 1 "
                 "AND upper(s.drug_name) = 'ZENOVAX'")
        params: tuple = ()
        if territory:
            where += (" AND EXISTS (SELECT 1 FROM organizations o JOIN zip_territory z "
                      "ON z.zip = o.zip WHERE o.org_id = s.org_id AND z.territory_name = %s)")
            params = (territory,)
        cur.execute(f"SELECT s.period_mo, sum(s.pack_units) AS v FROM sales s WHERE {where} "
                    "GROUP BY s.period_mo", params)
        sums = {r["period_mo"]: float(r["v"]) for r in cur.fetchall()}
    out, prior = {}, None
    for month, covered in months:                      # oldest first
        value = sums.get(month, 0.0) if covered else None
        change = value - prior if value is not None and prior is not None else None
        pct = change / prior if change is not None and prior > 0 else None
        out[month] = {"value": value, "prior": prior, "change": change, "pct": pct}
        prior = value
    return out


def table(result) -> dict[str, dict[str, float | None]]:
    f = lambda v: None if v is None else float(v)  # noqa: E731
    return {row["dim0_id"]: {"value": f(row["value"]), "prior": f(row["prior"]),
                             "change": f(row["change"]), "pct": f(row["change_pct"])}
            for row in result.answer.table}


def same(actual, expected):
    for month, got in actual.items():
        for field, value in got.items():
            want = expected[month][field]
            assert value == want or (value is not None and want is not None
                                     and abs(value - want) < 1e-6), (month, field, got, expected[month])


def test_the_sample_question_is_answered_month_by_month_with_each_change(pipeline, exec_user):
    result = pipeline.ask(exec_user, QUESTION)
    assert result.status == "answered", (result.status, result.message)
    assert result.answer.dimensions == ["period_mo"]
    actual = table(result)
    assert len(actual) >= 3
    same(actual, oracle(None))
    assert all(r["change"] is not None for r in actual.values()), \
        "every month of the window, including the first, has a prior in this dataset"
    assert "on the month before" in result.answer.headline
    latest = max(actual)
    word = "up" if actual[latest]["change"] > 0 else "down" if actual[latest]["change"] < 0 else "unchanged"
    assert f": {word}" in result.answer.headline, result.answer.headline


def test_the_current_month_is_marked_provisional_and_week_counts_are_shown(pipeline, exec_user):
    """The window includes the month still accumulating; its fall against a
    full month is partly missing weeks. The headline says the change is
    provisional, and every row carries both months' week counts, from the
    calendar."""
    from app.db import owner_transaction
    result = pipeline.ask(exec_user, QUESTION)
    with owner_transaction() as cur:
        cur.execute("SELECT period_mo, count(*) AS weeks FROM app_ref.calendar GROUP BY 1")
        weeks = {r["period_mo"]: r["weeks"] for r in cur.fetchall()}
        cur.execute("SELECT period_mo FROM app_ref.calendar WHERE wk_offset = 0")
        current = cur.fetchone()["period_mo"]
    rows = {r["dim0_id"]: r for r in result.answer.table}
    months = sorted(weeks)
    for month, row in rows.items():
        assert row["weeks"] == weeks[month]
        assert row["prior_weeks"] == weeks[months[months.index(month) - 1]]
        assert row["provisional"] is (month == current)
    assert current in rows and "provisional" in result.answer.headline


def test_a_territory_user_gets_their_territorys_changes_only(pipeline, ram_user):
    result = pipeline.ask(ram_user, QUESTION)
    assert result.status == "answered", (result.status, result.message)
    actual = table(result)
    expected = oracle(ram_user.territory_name)
    same(actual, expected)
    assert any(r["value"] for r in actual.values()), "the territory has Zenovax volume"
    overall = oracle(None)
    assert any(actual[m]["value"] != overall[m]["value"] for m in actual), \
        "the scoped series differs from the national one"
