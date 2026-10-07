"""Invariants on data that looks nothing like the supplied dataset.

Each profile (scripts/fixture_profile.py) keeps the supplied table definitions
and changes everything else: names, identifier formats, geography, hierarchy,
cardinality, skew, source coverage, classification, the reporting anchor and
an ingestion series. Each is built into a disposable database through the
ordinary bootstrap, load and ingestion (scripts/build_profile_db.py), and the
answers are compared with SQL written here, for an Exec, a director and a RAM
whose scopes are nonempty by construction.

Which numbers are checked and why: docs/COVERAGE_BY_INVARIANT.md.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from app.analytics.compiler import Compiler
from app.analytics.plan import AnalyticalPlan
from app.analytics.validator import validate
from tests.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]
ROOT = pathlib.Path(__file__).resolve().parents[2]
ALL = {"kind": "named", "named": "all_time"}
PAID = "s.data_source = 'distributor' AND s.brand_flag = 1"


def database(profile: str) -> str:
    return f"{os.environ.get('PAC_PROFILE_DB_PREFIX', 'pac_profile_')}{profile}"


@pytest.fixture(scope="module", params=["orchard", "estuary"])
def profile(request, tmp_path_factory):
    """Build the profile, point settings and pools at it, and restore after."""
    from app.analytics.entities import clear_caches
    from app.config import get_settings
    from app.db import close_pools

    name = request.param
    directory = tmp_path_factory.mktemp(f"profile-{name}")
    report = directory / "onboarding-report.json"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_profile_db.py"),
                        "--profile", name, "--db", database(name), "--dir", str(directory / "data"),
                        "--report", str(report)], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
    original = os.environ.get("PAC_DB_NAME")
    os.environ["PAC_DB_NAME"] = database(name)
    get_settings.cache_clear()
    close_pools()
    clear_caches()
    try:
        yield {"name": name, "dir": directory / "data", "report": json.loads(report.read_text()),
               "manifest": json.loads((directory / "data" / "manifest.json").read_text())}
    finally:
        close_pools()
        if original is None:
            os.environ.pop("PAC_DB_NAME", None)
        else:
            os.environ["PAC_DB_NAME"] = original
        get_settings.cache_clear()
        clear_caches()


def sql(text, params=()):
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute(text, params)
        return cur.fetchall()


def anchor():
    [row] = sql("SELECT reporting_anchor FROM app_meta.dataset_manifest d "
                "JOIN app_ref.generation g ON g.dataset_id = d.dataset_id")
    return row["reporting_anchor"]


def run(plan, scope=("global", None), wac=True):
    from app.db import analytics_transaction
    query = Compiler().compile(AnalyticalPlan.model_validate(plan), anchor=anchor())
    validate(query.sql, wac_authorized=wac)
    with analytics_transaction(scope_kind=scope[0], scope_value=scope[1], wac_authorized=wac) as cur:
        cur.execute(query.sql, query.params)
        return cur.fetchall()


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b)))


def user(role):
    from app.auth.policy import principal_for_user_id
    [row] = sql("SELECT user_id FROM users WHERE role = %s AND user_id LIKE 'PX%%' "
                "ORDER BY user_id LIMIT 1", (role,))
    return principal_for_user_id(row["user_id"])


def test_onboarding_reconciles_every_event_and_the_total(profile):
    rec = profile["report"]["reconciliation"]
    assert rec["every_event_reconciled"], [e for e in profile["report"]["events"] if not e["reconciled"]]
    assert rec["categories_cover_every_event"] and rec["totals_reconcile"], rec
    assert rec["anchor_after"] > rec["anchor_before"], "the sale after the base data moved the anchor"
    assert profile["report"]["real_data"] is False


def test_product_totals_match_hand_written_sums(profile):
    rows = run({"metric": "paid_pack_units", "dimensions": ["product"], "time": ALL})
    want = {r["drug_name"]: r["v"] for r in sql(
        f"SELECT s.drug_name, sum(s.pack_units) AS v FROM sales s WHERE {PAID} GROUP BY 1")}
    got = {r["dim0_label"]: r["value"] for r in rows}
    assert got and set(got) == set(want) and all(close(got[k], want[k]) for k in want)


def test_accounts_roll_up_to_their_top_organization_and_equal_names_stay_apart(profile):
    rows = run({"metric": "paid_pack_units", "dimensions": ["account"], "time": ALL})
    want = {r["acct"]: r["v"] for r in sql(
        f"SELECT coalesce(o.grandparent_org_id, o.org_id) AS acct, sum(s.pack_units) AS v "
        f"FROM sales s JOIN organizations o ON o.org_id = s.org_id WHERE {PAID} GROUP BY 1")}
    got = {r["dim0_id"]: r["value"] for r in rows}
    assert set(got) == set(want) and all(close(got[k], want[k]) for k in want)
    harbor = [r for r in rows if r["dim0_label"] == "Harbor Infusion"]
    assert len({r["dim0_id"] for r in harbor}) == len(harbor) and len(harbor) in (0, 2)


def test_territories_and_the_unmapped_remainder_partition_the_total(profile):
    rows = run({"metric": "paid_pack_units", "dimensions": ["territory"], "time": ALL})
    [total] = sql(f"SELECT sum(s.pack_units) AS v FROM sales s WHERE {PAID}")
    [unmapped] = sql(f"SELECT coalesce(sum(s.pack_units), 0) AS v FROM sales s "
                     f"JOIN organizations o ON o.org_id = s.org_id "
                     f"LEFT JOIN zip_territory z ON z.zip = o.zip WHERE {PAID} AND z.zip IS NULL")
    mapped = sum(float(r["value"]) for r in rows if r["dim0_id"] is not None)
    assert close(mapped + float(unmapped["v"]), total["v"])


@pytest.mark.parametrize("role", ["director", "ram"])
def test_a_scoped_role_sees_exactly_its_own_nonempty_scope(profile, role):
    principal = user(role)
    column = "region_name" if role == "director" else "territory_name"
    rows = run({"metric": "paid_pack_units", "time": ALL},
               scope=(principal.scope_kind, principal.scope_value), wac=False)
    [want] = sql(f"SELECT sum(s.pack_units) AS v FROM sales s JOIN organizations o "
                 f"ON o.org_id = s.org_id JOIN zip_territory z ON z.zip = o.zip "
                 f"WHERE {PAID} AND z.{column} = %s", (principal.scope_value,))
    assert want["v"] and close(rows[0]["value"], want["v"]), (principal.scope_value, rows, want)
    [total] = sql(f"SELECT sum(s.pack_units) AS v FROM sales s WHERE {PAID}")
    assert float(rows[0]["value"]) < float(total["v"]), "a scope is smaller than the whole"


def test_brand_share_of_our_market_uses_one_population(profile):
    company = sorted({r["drug_name"] for r in sql("SELECT drug_name FROM products WHERE brand_flag = 1")})
    rows = run({"metric": "brand_market_share", "filters": {"product_names": [company[0]]},
                "time": ALL})
    [want] = sql(
        "SELECT sum(s.pack_units * p.unit_conversion_factor) FILTER "
        "  (WHERE s.data_source = 'distributor' AND s.brand_flag = 1 AND s.drug_name = %s) AS n, "
        "sum(s.pack_units * p.unit_conversion_factor) FILTER (WHERE s.data_source = 'market_data') AS d "
        "FROM sales s JOIN products p ON p.ndc = s.ndc WHERE p.market_subcategory IN "
        "(SELECT market_subcategory FROM products WHERE brand_flag = 1 AND drug_name = %s)",
        (company[0], company[0]))
    assert close(rows[0]["value"], want["n"] / want["d"]), (rows, want)


def test_a_month_without_market_data_is_unknown_and_a_silent_month_is_zero(profile):
    assert profile["manifest"]["market_gap_weeks"], "every profile has a market-data gap"
    rows = run({"metric": "market_equivalents", "dimensions": ["period_mo"], "time": ALL})
    uncovered = {r["period_mo"] for r in sql(
        "SELECT period_mo FROM app_ref.calendar GROUP BY 1 "
        "HAVING NOT bool_and('market_data' = ANY(sources))")}
    got = {r["dim0_id"]: r["value"] for r in rows}
    assert uncovered and all(got[m] is None for m in uncovered)
    assert all(got[m] is not None for m in got if m not in uncovered)
    silent_ndc, silent_weeks = profile["manifest"]["silent_product"]
    [drug] = sql("SELECT drug_name FROM products WHERE ndc = %s", (silent_ndc,))
    months = {r["period_mo"] for r in sql(
        "SELECT period_mo FROM app_ref.calendar GROUP BY 1 HAVING bool_and(wk_offset = ANY(%s))",
        ([w + 1 for w in silent_weeks],))}   # one week later: the batch moved the anchor
    series = run({"metric": "paid_pack_units", "dimensions": ["period_mo"],
                  "filters": {"product_names": [drug["drug_name"]]}, "time": ALL})
    for row in series:
        if row["dim0_id"] in months:
            assert float(row["value"]) == 0.0, row


def test_each_month_against_the_one_before_matches_the_oracle(profile):
    company = sorted({r["drug_name"] for r in sql("SELECT drug_name FROM products WHERE brand_flag = 1")})
    rows = run({"metric": "paid_pack_units", "dimensions": ["period_mo"],
                "filters": {"product_names": [company[0]]}, "time": ALL, "period_over_period": True})
    months = [r["period_mo"] for r in sql(
        "SELECT period_mo FROM app_ref.calendar GROUP BY 1 ORDER BY min(week_ending_date)")]
    sums = {r["m"]: float(r["v"]) for r in sql(
        f"SELECT s.period_mo AS m, sum(s.pack_units) AS v FROM sales s WHERE {PAID} "
        f"AND s.drug_name = %s GROUP BY 1", (company[0],))}
    got = {r["dim0_id"]: r for r in rows}
    prior = None
    for month in months:
        value = sums.get(month, 0.0)
        assert close(got[month]["value"], value) and close(got[month]["prior_value"], prior), month
        prior = value


def test_a_reused_territory_name_is_refused_at_load(profile, tmp_path):
    """Scope binds by territory name (migrations/004_security.sql). A feed that
    reuses one name for two territories would let a RAM assigned that name
    see both. The load must refuse it by name, not publish a widened scope."""
    from app.data.loader import LoadError, load

    data = tmp_path / "dup"
    shutil.copytree(profile["dir"], data)
    lines = (data / "zip_territory.csv").read_text().splitlines()
    first_name = lines[1].split(",")[3]
    renamed = [lines[0]] + [
        line if i < 6 else ",".join(p if j != 3 else first_name for j, p in enumerate(line.split(",")))
        for i, line in enumerate(lines[1:], 1)]
    assert len({r.split(",")[2] for r in renamed[1:] if r.split(",")[3] == first_name}) > 1
    (data / "zip_territory.csv").write_text("\n".join(renamed) + "\n")
    with pytest.raises(LoadError, match="territory name"):
        load("full", generated_dir=data)


ELIMINATION = ("classification rule 1.0.0 classes a product by elimination: brand_flag 0 "
               "and no generic or biosimilar name suffix makes it a branded competitor, "
               "whatever it is, and no mapping can say otherwise")


@pytest.mark.xfail(strict=True, reason=ELIMINATION)
def test_a_product_nothing_classifies_stays_unknown(profile):
    """The source's brand_flag says what is ours; the dataset's mapping says
    what the rest are. A product neither names is unknown -- not a branded
    competitor because its name lacks a suffix the supplied data happened to
    use. Orchard ships a mapping without its unknown products; estuary ships
    none."""
    expected = profile["manifest"]["expected_classification"]
    got = {r["ndc"]: r["classification"] for r in sql(
        "SELECT ndc, classification FROM app_ref.product_classification")}
    assert "unknown" in expected.values()
    assert got == expected, {n: (got.get(n), c) for n, c in expected.items() if got.get(n) != c}


@pytest.mark.xfail(strict=True, reason=ELIMINATION)
def test_a_segment_share_counts_unknown_volume_in_no_segment_and_bounds_it(profile):
    """Branded-competitor share of each of our markets. The segment is the
    products known to be branded competitors; volume of unknown class is in
    the market and in no segment, and the answer carries it, so the share
    can be stated as the range that volume allows. A market whose volume is
    all of unknown class has no share to report."""
    expected = profile["manifest"]["expected_classification"]
    rows = run({"metric": "market_segment_share", "dimensions": ["market_subcategory"],
                "filters": {"classifications": ["branded_competitor"]}, "time": ALL})
    want: dict[str, dict[str, float]] = {}
    for r in sql("SELECT p.market_subcategory AS sub, p.ndc, "
                 "sum(s.pack_units * p.unit_conversion_factor) AS v FROM sales s "
                 "JOIN products p ON p.ndc = s.ndc WHERE s.data_source = 'market_data' "
                 "AND p.market_subcategory IN (SELECT market_subcategory FROM products "
                 "WHERE brand_flag = 1) GROUP BY 1, 2"):
        w = want.setdefault(r["sub"], {"n": 0.0, "u": 0.0, "d": 0.0})
        w["d"] += float(r["v"])
        w["n"] += float(r["v"]) if expected[r["ndc"]] == "branded_competitor" else 0.0
        w["u"] += float(r["v"]) if expected[r["ndc"]] == "unknown" else 0.0
    assert any(w["u"] for w in want.values()), "the profile has market volume of unknown class"
    got = {r["dim0_id"]: r for r in rows}
    for sub, w in want.items():
        row = got[sub]
        if w["u"] >= w["d"]:
            assert row["value"] is None and row["value_upper"] is None, (sub, row)
            continue
        assert close(row["value"], w["n"] / w["d"]), (sub, row, w)
        assert close(row["unclassified"], w["u"]), (sub, row, w)
        assert close(row["value_upper"], (w["n"] + w["u"]) / w["d"]), (sub, row, w)
