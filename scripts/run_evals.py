#!/usr/bin/env python3
"""Run the held-out evaluation set.

  python3 scripts/run_evals.py                       # offline planner, free
  python3 scripts/run_evals.py --provider bedrock    # COSTS MONEY

Two things this deliberately does NOT do:

  * It does not grade with a language model. Expected answers come from
    reference SQL written by hand in evals/questions.yaml, or from structural
    assertions about the produced plan.
  * It does not present an offline run as natural-language accuracy. The
    offline planner is a keyword matcher; an offline run checks that the
    compiler, authorization, execution and rendering layers work, and the
    report says so on its face.

Every run writes a timestamped JSON record to evals/runs/ containing the
question, principal, dataset id, metric and policy versions, the produced plan
and SQL, the expected and actual values, pass/fail with a reason, latency and
token usage where available.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "evals" / "questions.yaml"
RUNS = ROOT / "evals" / "runs"


# ---------------------------------------------------------------------------
# Reference answers -- computed independently of the system under test
# ---------------------------------------------------------------------------

def reference(sql: str, principal) -> Any:
    from app.db import analytics_transaction

    with analytics_transaction(
        scope_kind=principal.scope_kind,
        scope_value=principal.scope_value,
        wac_authorized=principal.wac_authorized,
    ) as cur:
        cur.execute(sql)
        return cur.fetchall()


def close(a: Any, b: Any, tolerance: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / scale <= tolerance


# ---------------------------------------------------------------------------
# Judgements
# ---------------------------------------------------------------------------

def judge(spec: dict, result, principal, tolerance: float) -> tuple[bool, str]:
    kind = spec["type"]
    answer = result.answer

    if kind == "denied":
        if result.status != "denied":
            return False, f"expected a refusal, got {result.status}"
        if not result.alternative:
            return False, "refused without offering an alternative"
        return True, "refused with an alternative"

    if kind == "warning":
        if not answer:
            return False, f"no answer ({result.status})"
        needle = spec.get("contains", "").lower()
        joined = " ".join(answer.warnings).lower()
        if needle and needle not in joined:
            return False, f"no warning containing {needle!r}"
        return True, "data-quality warning present"

    if kind == "nonempty":
        if result.status != "answered" or not answer:
            return False, f"status {result.status}"
        if answer.row_count == 0:
            return False, "no rows"
        if "max_rows" in spec and answer.row_count > spec["max_rows"]:
            return False, f"{answer.row_count} rows exceeds max {spec['max_rows']}"
        return True, f"{answer.row_count} row(s)"

    if kind == "volume_alternative":
        # Either refuse with an alternative, or answer in volume and say why.
        if result.status == "denied" and result.alternative:
            return True, "refused with a volume alternative"
        if result.status == "answered" and answer:
            text = " ".join(answer.notes).lower()
            if "pricing" in text or "restricted" in text:
                if "usd" in (answer.columns[-1] or "").lower():
                    return False, "returned currency to a non-Exec"
                return True, "answered in volume, restriction disclosed"
            return False, "answered without disclosing the pricing restriction"
        return False, f"status {result.status}"

    if kind == "no_pricing":
        if result.sql and "wac" in result.sql.lower():
            return False, "SQL CONTAINS WAC"
        if answer and any("$" in str(r.get("value_formatted", "")) for r in answer.table):
            return False, "response contains currency"
        return True, "no pricing reached the response"

    if kind == "plan":
        if not result.plan:
            return False, f"no plan produced ({result.status})"
        for key, expected in spec["plan"].items():
            actual = result.plan.get(key)
            if isinstance(expected, dict):
                for sub, value in expected.items():
                    if (actual or {}).get(sub) != value:
                        return False, f"plan.{key}.{sub} = {(actual or {}).get(sub)!r}, expected {value!r}"
            elif isinstance(expected, list):
                if list(actual or []) != expected:
                    return False, f"plan.{key} = {actual!r}, expected {expected!r}"
            elif actual != expected:
                return False, f"plan.{key} = {actual!r}, expected {expected!r}"
        return True, "plan matches"

    if kind == "any_of":
        allowed = set(spec["allowed"])
        if result.status in allowed:
            return True, result.status
        if "answered_with_note" in allowed and result.status == "answered":
            if answer and (answer.notes or answer.warnings):
                return True, "answered with a qualifying note"
        return False, f"status {result.status} not in {sorted(allowed)}"

    if kind == "sql":
        if result.status != "answered" or not answer:
            return False, f"status {result.status}"
        rows = reference(spec["sql"], principal)
        compare = spec.get("compare", "scalar")

        if compare == "scalar":
            expected = list(rows[0].values())[0] if rows else None
            actual = answer.table[0]["value"] if answer.table else None
            ok = close(expected, actual, tolerance)
            return ok, f"expected {expected!r}, got {actual!r}"

        if compare == "top_ids":
            expected_ids = [list(r.values())[0] for r in rows]
            actual_ids = [r.get("dim0_id") for r in answer.table][: len(expected_ids)]
            # Compare as sets: equal values may tie in either order.
            if set(expected_ids) == set(actual_ids):
                return True, f"{len(expected_ids)} ids match"
            missing = set(expected_ids) - set(actual_ids)
            return False, f"missing {sorted(missing)[:5]}"

        if compare == "rows":
            expected_map = {str(r["label"]): r["value"] for r in rows}
            actual_map = {
                str(r.get("dim0")): r.get("value") for r in answer.table
            }
            for label, value in expected_map.items():
                if label not in actual_map:
                    return False, f"missing group {label!r}"
                if not close(value, actual_map[label], tolerance):
                    return False, f"{label}: expected {value!r}, got {actual_map[label]!r}"
            return True, f"{len(expected_map)} group(s) match"

    return False, f"unknown expectation type {kind!r}"


def judge_turn(spec: dict, result, previous) -> tuple[bool, str]:
    kind = spec["type"]
    if kind == "keeps_dimension":
        dims = (result.plan or {}).get("dimensions", [])
        return (spec["dimension"] in dims,
                f"dimensions {dims}, expected to still contain {spec['dimension']!r}")
    if kind == "keeps_ranking":
        ranking = (result.plan or {}).get("ranking")
        if not ranking:
            return False, "ranking was dropped by the follow-up"
        return (ranking.get("limit") == spec["limit"],
                f"limit {ranking.get('limit')}, expected {spec['limit']}")
    if kind == "frozen_cohort":
        ids = ((result.plan or {}).get("filters") or {}).get("account_ids") or []
        if not ids:
            return False, "cohort was not frozen into account_ids"
        if (result.plan or {}).get("ranking"):
            return False, "frozen cohort was re-ranked"
        return True, f"cohort frozen to {len(ids)} ids"
    return judge(spec, result, None, 1e-9)


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("offline", "bedrock"), default="offline")
    ap.add_argument("--tolerance", type=float, default=1e-9)
    ap.add_argument("--family", help="run only one family")
    ap.add_argument("--id", dest="only", help="run only one question id")
    args = ap.parse_args()

    if args.provider == "bedrock":
        print("Running against AWS Bedrock. THIS COSTS MONEY.\n")

    import os

    os.environ["PAC_LLM_PROVIDER"] = args.provider
    from app.config import get_settings

    get_settings.cache_clear()

    from app.auth.policy import principal_for_user_id
    from app.db import close_pools, owner_transaction
    from app.llm.planner import build_planner
    from app.pipeline import Pipeline

    spec = yaml.safe_load(QUESTIONS.read_text())
    questions = spec["questions"]
    if args.family:
        questions = [q for q in questions if q.get("family") == args.family]
    if args.only:
        questions = [q for q in questions if q["id"] == args.only]

    # Throwaway credentials, one per role, generated per run.
    with owner_transaction() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (u.role) u.user_id, u.email, u.role
            FROM users u
            ORDER BY u.role,
                     CASE WHEN u.role = 'exec' THEN 1
                          WHEN u.role = 'director' AND EXISTS (
                               SELECT 1 FROM zip_territory z
                               WHERE z.region_name = u.region_name) THEN 1
                          WHEN u.role = 'ram' AND EXISTS (
                               SELECT 1 FROM zip_territory z
                               WHERE z.territory_name = u.territory_name) THEN 1
                          ELSE 2 END,
                     u.user_id
            """
        )
        rows = cur.fetchall()
    # Built directly from the users table -- no password is minted, so running
    # this never disturbs credentials that have been issued to anyone.
    principals = {row["role"]: principal_for_user_id(row["user_id"]) for row in rows}

    planner = build_planner()
    pipeline = Pipeline(planner)
    dataset = pipeline.current_dataset()

    from app.analytics.registry import get_registry

    results: list[dict[str, Any]] = []
    passed = failed = 0

    for item in questions:
        principal = principals[item["principal"]]
        turns = item.get("turns") or [{"question": item["question"], "expect": item["expect"]}]
        conversation_id = None
        previous = None

        for index, turn in enumerate(turns):
            started = time.perf_counter()
            result = pipeline.ask(
                principal, turn["question"],
                conversation_id=conversation_id, include_sql=True,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            conversation_id = result.conversation_id

            expect = turn["expect"]
            tolerance = expect.get("tolerance", args.tolerance)
            try:
                if index > 0 and expect["type"] in (
                    "keeps_dimension", "keeps_ranking", "frozen_cohort"
                ):
                    ok, reason = judge_turn(expect, result, previous)
                else:
                    ok, reason = judge(expect, result, principal, tolerance)
            except Exception as exc:                      # a broken expectation is a failure
                ok, reason = False, f"{type(exc).__name__}: {exc}"

            passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
            label = f"{item['id']}" + (f".{index + 1}" if len(turns) > 1 else "")
            mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
            print(f"  {mark}  {label:<10} {turn['question'][:62]:<62} {reason[:60]}")

            usage = getattr(planner, "last_usage", None) or {}
            results.append({
                "id": label,
                "family": item.get("family"),
                "principal": {
                    "role": principal.role,
                    "scope": principal.scope_value,
                    "wac_authorized": principal.wac_authorized,
                },
                "question": turn["question"],
                "status": result.status,
                "passed": ok,
                "reason": reason,
                "plan": result.plan,
                "sql": result.sql,
                "latency_ms": elapsed_ms,
                "timings": result.timings,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            })
            previous = result

    total = passed + failed
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = {
        "run_at": stamp,
        "provider": args.provider,
        "model_id": getattr(planner, "model_id", "offline-deterministic-planner"),
        "measures_nl_accuracy": args.provider == "bedrock",
        "dataset_id": dataset["dataset_id"],
        "dataset_mode": dataset["load_mode"],
        "question_set_version": spec["version"],
        "metric_version": get_registry().version,
        "policy_version": "1.0.0",
        "total": total, "passed": passed, "failed": failed,
        "results": results,
    }
    path = RUNS / f"{stamp}-{args.provider}.json"
    path.write_text(json.dumps(record, indent=2, default=str))

    print(f"\n  {passed}/{total} passed" + (f", {failed} failed" if failed else ""))
    print(f"  written to {path.relative_to(ROOT)}")
    if args.provider == "offline":
        print(
            "\n  NOTE: the offline planner is a deterministic keyword matcher.\n"
            "  This run exercises the compiler, authorization, execution and\n"
            "  rendering layers. It is NOT a measurement of natural-language\n"
            "  accuracy and must not be reported as one."
        )
    close_pools()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
