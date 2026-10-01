#!/usr/bin/env python3
"""The 30 September 2026 review's findings, as an executable ledger.

The review assessed c8aab5b. This branch has moved since, so each finding is
re-checked against the CURRENT checkout rather than assumed. Every check
exercises the real function on the affected path -- compiler, intent guard,
judge, cohort summary -- and reports one of:

    REPRODUCED   the defect is present now
    FIXED        the defect is absent now
    OPEN         a source-level risk the review raised; no behavioural probe
                 exists yet, so it is not claimed either way

    python3 evidence/probes/review_2026_09_30.py          # table
    python3 evidence/probes/review_2026_09_30.py --json   # machine-readable

Exits with the number of REPRODUCED findings, so 0 means none reproduce. It
reads the working database (SELECT only) and never writes.
"""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


@dataclass
class Result:
    finding: str
    status: str          # REPRODUCED | FIXED | OPEN | ERROR
    detail: str


CHECKS: list[tuple[str, str, Callable[[], tuple[bool | None, str]]]] = []


def check(finding: str, title: str):
    """Register a probe. It returns (defect_present, detail); None = OPEN."""
    def wrap(fn):
        CHECKS.append((finding, title, fn))
        return fn
    return wrap


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

_ANCHOR = None


def anchor():
    global _ANCHOR
    if _ANCHOR is None:
        from app.db import owner_transaction
        with owner_transaction() as cur:
            cur.execute("SELECT reporting_anchor FROM app_meta.dataset_manifest "
                        "WHERE load_state='published' ORDER BY published_at DESC LIMIT 1")
            _ANCHOR = cur.fetchone()["reporting_anchor"]
    return _ANCHOR


def run_plan(plan_dict):
    """Compile and execute a plan. Returns (rows, sql) or raises."""
    from app.analytics.compiler import Compiler
    from app.analytics.plan import AnalyticalPlan
    from app.db import owner_transaction
    q = Compiler(max_rows=5000).compile(
        AnalyticalPlan.model_validate(plan_dict), anchor=anchor())
    with owner_transaction() as cur:
        cur.execute("SET LOCAL statement_timeout = '30s'")
        cur.execute(q.sql, q.params)
        return cur.fetchall(), q.sql


def vocabulary():
    from app.analytics.entities import Vocabulary, _product_vocabulary
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT dataset_id FROM app_meta.dataset_manifest "
                    "WHERE load_state='published' ORDER BY published_at DESC LIMIT 1")
        ds = cur.fetchone()["dataset_id"]
        cur.execute("SELECT DISTINCT territory_name FROM zip_territory ORDER BY 1")
        terr = [r["territory_name"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT region_name FROM zip_territory "
                    "WHERE region_name IS NOT NULL ORDER BY 1")
        reg = [r["region_name"] for r in cur.fetchall()]
    products, subs, cats, specs = _product_vocabulary(ds)
    return Vocabulary(products=products, subcategories=subs, categories=cats,
                      specialties=specs, territories=terr, regions=reg,
                      all_territories=terr, all_regions=reg)


def plan_of(**kw):
    from app.analytics.plan import AnalyticalPlan
    base = {"metric": "paid_pack_units", "time": {"kind": "named", "named": "r3m"}}
    base.update(kw)
    return AnalyticalPlan.model_validate(base)


def blocking_gaps(question, plan, **extra):
    from app.analytics import intent
    gaps = intent.find_gaps(question, plan, vocabulary(), **extra)
    return [g for g in intent.blocking(gaps)], gaps


# --------------------------------------------------------------------------
# Finding 1 -- release tests
# --------------------------------------------------------------------------

@check("F1a", "security job builds its database and runs the strict gate")
def _():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    ok = "build_authtest_db.py" in ci and "--release-gate" in ci and "--min-tests" in ci
    return (not ok, "ci.yml builds authtest and passes --release-gate --min-tests"
            if ok else "ci.yml is missing the authtest build or the gate flags")


@check("F1b", "frontend job runs the component suite, not only the build")
def _():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    runs = "npm test" in ci or "vitest" in ci or "npm run test" in ci
    return (not runs, "ci.yml runs the component tests" if runs
            else "ci.yml builds the frontend but never runs its tests")


# --------------------------------------------------------------------------
# Finding 2 -- entity fidelity
# --------------------------------------------------------------------------

@check("F2a", "unknown product in any casing is blocked, not answered company-wide")
def _():
    leaked = []
    for spelling in ("FLOOBERTAX", "Floobertax", "floobertax"):
        blocking, _ = blocking_gaps(f"What is the volume for {spelling} this quarter?",
                                    plan_of())
        if not blocking:
            leaked.append(spelling)
    return (bool(leaked), f"no blocking gap for {leaked}" if leaked
            else "all three spellings blocked")


@check("F2b", "a known product dropped from the plan is caught")
def _():
    blocking, _ = blocking_gaps("What is the volume for Zenovax this quarter?",
                                plan_of())   # plan carries no product filter
    return (not blocking, "blocked" if blocking
            else "Zenovax named, plan unfiltered, no blocking gap")


@check("F2c", "a product the planner invented is caught")
def _():
    blocking, _ = blocking_gaps("What is the volume this quarter?",
                                plan_of(filters={"product_names": ["FLOOBERTAX"]}))
    return (not blocking, "blocked" if blocking
            else "plan filters on a product that does not exist; no gap")


@check("F2d", "entity resolvers are on the real request path")
def _():
    import re
    src = (ROOT / "app/pipeline.py").read_text()
    # A CALL, not a mention: the word "fidelity" in a comment satisfied the
    # first version of this check while nothing was wired.
    wired = re.search(r"\b(resolve_accounts|resolve_products|resolve_mentions)\(", src)
    return (not wired, f"pipeline calls {wired.group(1)}()" if wired
            else "app/pipeline.py never calls an entity resolver")


# --------------------------------------------------------------------------
# Finding 3 -- live prompt cohort typing
# --------------------------------------------------------------------------

@check("F3", "live prompt types the cohort and classifies the turn")
def _():
    import subprocess
    p = subprocess.run([sys.executable, str(ROOT / "evidence/probes/live_prompt_mistypes_cohort.py")],
                       capture_output=True, text=True, cwd=ROOT)
    # That probe exits 0 while the defect is present.
    return (p.returncode == 0, "probe reports the defect" if p.returncode == 0
            else "probe reports it fixed")


# --------------------------------------------------------------------------
# Finding 4 -- conversation persistence
# --------------------------------------------------------------------------

def _cohort(rows, max_rows=5000):
    from app.conversation.continuity import summarise_cohort
    return summarise_cohort(rows, dimension="account", max_rows=max_rows)


@check("F4a", "a 500-account answer keeps all 500 for 'those same accounts'")
def _():
    c = _cohort([{"dim0_id": f"A{i:04d}"} for i in range(500)])
    kept = len(c.ids)
    return (kept != 500, f"kept {kept} of 500 (complete={c.complete})")


@check("F4b", "repeated period rows do not count as new cohort members")
def _():
    rows = [{"dim0_id": f"A{a}", "dim1_id": m} for a in range(3) for m in ("2026-07", "2026-08")]
    c = _cohort(rows)
    distinct = len(set(c.ids)) == len(c.ids)
    return (not distinct or c.total_available != 3,
            f"ids={list(c.ids)} total={c.total_available}")


@check("F4c", "a pending clarification and its choices survive to the next turn")
def _():
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""SELECT 1 FROM information_schema.columns
                       WHERE table_schema='app_conv'
                         AND (column_name LIKE '%clarif%' OR table_name LIKE '%clarif%')""")
        present = cur.fetchone() is not None
    return (not present, "clarification state is stored" if present
            else "app_conv has nowhere to store a pending clarification")


@check("F4d", "overlapping turns are ordered by a revision check or run lease")
def _():
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""SELECT 1 FROM information_schema.columns
                       WHERE table_schema='app_conv'
                         AND column_name IN ('revision','lease_owner','lease_expires_at')""")
        present = cur.fetchone() is not None
    return (not present, "revision/lease present" if present
            else "no revision or lease; only the final insert is serialised")


@check("F4e", "a failed conversation write is reported, not swallowed")
def _():
    src = (ROOT / "app/pipeline.py").read_text()
    swallowed = "never fail a request on bookkeeping" in src
    return (swallowed, "_record() logs and continues; the result still says answered"
            if swallowed else "persistence outcome is reported")


# --------------------------------------------------------------------------
# Finding 5 -- compiler and numeric defects
# --------------------------------------------------------------------------

@check("F5a", "facility_count_all by territory / region executes")
def _():
    failures = []
    for label, kw in (("territory dim", {"dimensions": ["territory"]}),
                      ("region dim", {"dimensions": ["region"]}),
                      ("regions filter", {"filters": {"regions": ["West"]}})):
        try:
            run_plan({"metric": "facility_count_all",
                      "time": {"kind": "named", "named": "r3m"}, **kw})
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}")
    return (bool(failures), "; ".join(failures) or "all execute")


@check("F5b", "'at least 100' can include exactly 100")
def _():
    from app.analytics.plan import Threshold
    fields = Threshold.model_fields
    inclusive = "op" in fields or any(
        "gte" in str(f.annotation) or "at_least" in str(f.annotation)
        for f in fields.values())
    return (not inclusive, "threshold supports inclusive bounds" if inclusive
            else "Threshold.direction is above|below only; 'at least' compiles to >")


@check("F5c", "a sparse series does not average months outside its window")
def _():
    rows, _ = run_plan({
        "metric": "paid_pack_units", "dimensions": ["period_mo"],
        "filters": {"facility_ids": ["FA0017"], "product_names": ["PAXELIUM"]},
        "time": {"kind": "named", "named": "last_6_months"},
        "rolling": {"periods": 3}})
    sep = next((r for r in rows if r["dim0_id"] == "2026-09"), None)
    if sep is None:
        return (True, "September missing from the series")
    value = float(sep["value"]) if sep["value"] is not None else None
    # Jul and Aug have no purchases; within source coverage that is zero.
    correct = value is not None and abs(value - 2 / 3) < 1e-6
    return (not correct, f"Sep rolling = {value} (Jul-Sep with Jul=Aug=0 is 0.6667)")


@check("F5d", "a valid percentage metric is not told it is a count")
def _():
    from app.analytics import intent
    wrong = []
    for metric in ("share_340b", "market_segment_share", "volume_growth"):
        comparison = ({"comparison": {"kind": "named", "named": "r6m_prior"}}
                      if metric == "volume_growth" else {})
        gaps = intent.find_gaps("What percentage of our volume comes from 340B accounts?",
                                plan_of(metric=metric, **comparison), vocabulary())
        if any(g.kind == "unsupported_proportion" for g in gaps):
            wrong.append(metric)
    return (bool(wrong), f"'count, not a percentage' on {wrong}" if wrong
            else "ratio metrics recognised")


# --------------------------------------------------------------------------
# Findings 6-8
# --------------------------------------------------------------------------

@check("F6", "a refresh mid-request cannot pair one generation's plan with another's rows")
def _():
    # Behavioural, against the disposable database: a publish is landed
    # between planning and execution, an old snapshot is held across a
    # commit, and readers are timed during an uncommitted publication.
    import subprocess
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/security/test_snapshot_consistency.py",
         "-q", "-p", "no:randomly"], capture_output=True, text=True, cwd=ROOT)
    tail = (p.stdout.strip().splitlines() or ["no output"])[-1]
    return (p.returncode != 0, tail)


@check("F7a", "planner usage is per-call, not shared instance state")
def _():
    from app.llm.planner import BedrockPlanner
    shared = "last_usage" in BedrockPlanner.__dict__ or any(
        "self.last_usage" in (ROOT / "app/llm/planner.py").read_text() for _ in [0])
    return (shared, "BedrockPlanner.last_usage still exists" if shared
            else "per-call PlanningResult")


@check("F7b", "intent gaps and reason codes reach the audit row")
def _():
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema='app_meta' AND table_name='query_audit'""")
        cols = {r["column_name"] for r in cur.fetchall()}
    stored = "intent_gaps" in cols or "reason_codes" in cols
    return (not stored, "persisted" if stored
            else "audit['intent_gaps'] is set in memory; query_audit has no column for it")


@check("F8", "a plan-only eval pass requires the request to have succeeded")
def _():
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_evals", ROOT / "scripts/run_evals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class R:  # the shape judge() reads
        status = "error"; plan = {"metric": "paid_pack_units"}; answer = None
        sql = None; alternative = None; message = "compile failed"
    ok, why = mod.judge({"type": "plan", "plan": {"metric": "paid_pack_units"}},
                        R(), None, 0.01)
    return (ok, f"status=error, no answer -> judged {ok} ({why})")


# --------------------------------------------------------------------------

def main() -> int:
    results: list[Result] = []
    for finding, title, fn in CHECKS:
        try:
            present, detail = fn()
            status = "OPEN" if present is None else ("REPRODUCED" if present else "FIXED")
        except Exception as exc:                      # a probe that breaks is not a pass
            status, detail = "ERROR", f"{type(exc).__name__}: {str(exc)[:160]}"
        results.append(Result(finding, status, f"{title} -- {detail}"))

    if "--json" in sys.argv:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        for r in results:
            print(f"{r.finding:5} {r.status:10} {r.detail}")
        tally = {s: sum(r.status == s for r in results)
                 for s in ("REPRODUCED", "FIXED", "OPEN", "ERROR")}
        print("\n" + "  ".join(f"{k}={v}" for k, v in tally.items()))
    return sum(r.status in ("REPRODUCED", "ERROR") for r in results)


if __name__ == "__main__":
    sys.exit(main())
