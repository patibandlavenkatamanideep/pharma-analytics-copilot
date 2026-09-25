#!/usr/bin/env python3
"""Run the regression evaluation set.

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
  * It does not present this set as held out. The questions were used during
    development and the system was changed in response to them, so a score
    here is regression coverage, not an accuracy estimate. See the header of
    evals/questions.yaml.

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

class SpecificationError(ValueError):
    """The expectation itself is unusable, so the question cannot be scored.

    Raised rather than returned as a failure: a question that can never pass
    and a question that is written wrong are different problems, and only one
    of them is the system's fault.
    """


def _text_of(answer) -> str:
    """Everything in an answer a person would actually read."""
    parts = [answer.headline, answer.scope_note, answer.period_note]
    parts += list(answer.notes) + list(answer.warnings)
    for row in answer.table:
        parts += [str(v) for v in row.values()]
    return " ".join(str(p) for p in parts if p)


# What a passing check actually demonstrates. Reported separately because
# "38/38 passed" conflates four different things, and only the first is
# question-answering capability: correctly REFUSING an unsupported request is
# right behaviour and is not evidence that the system can compute it.
CATEGORIES = {
    "answer": "Correct business answers",
    "refusal": "Correct authorization refusals",
    "unsupported": "Correct clarifications / unsupported requests",
    "wrong": "Incorrect answers",
    "failure": "Execution failures",
}


def categorise(spec: dict, result, ok: bool) -> str:
    """Bucket one outcome. Failures split by whether the system broke."""
    if not ok:
        return "failure" if result.status == "error" else "wrong"
    if result.status == "denied":
        return "refusal"
    if result.status == "clarify":
        return "unsupported"
    if result.status == "error":
        return "failure"
    # Answered, and accepted. If the expectation would also have accepted a
    # clarification or a refusal, this was an unsupported request handled
    # gracefully -- not a demonstration that the metric can be computed.
    if spec.get("type") == "any_of" and (
            {"clarify", "denied"} & set(spec.get("allowed", []))):
        return "unsupported"
    if spec.get("type") == "volume_alternative":
        return "refusal"
    return "answer"


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
        # An expectation with no needle passed any answer, including one
        # carrying no warning at all. There is no useful reading of
        # "must warn about nothing in particular".
        needle = spec.get("contains", "").strip().lower()
        if not needle:
            raise SpecificationError(
                "type: warning requires `contains:` naming the warning expected")
        if not answer:
            return False, f"no answer ({result.status})"
        joined = " ".join(answer.warnings).lower()
        if needle not in joined:
            return False, f"no warning containing {needle!r} (warnings: {answer.warnings})"
        return True, f"warning containing {needle!r}"

    if kind == "nonempty":
        # Coverage only, and only for a NAMED metric. Without that this was a
        # row counter: b340-01 asked what percentage of volume comes from 340B
        # accounts and passed on 59,419 packs, because packs are rows too.
        expected_metric = spec.get("metric")
        if not expected_metric:
            raise SpecificationError(
                "type: nonempty requires `metric:` -- otherwise any answer with "
                "rows passes, whatever it measured")
        if result.status != "answered" or not answer:
            return False, f"status {result.status}"
        if answer.row_count == 0:
            return False, "no rows"
        actual_metric = (result.plan or {}).get("metric")
        if actual_metric != expected_metric:
            return False, f"metric {actual_metric!r}, expected {expected_metric!r}"
        if "max_rows" in spec and answer.row_count > spec["max_rows"]:
            return False, f"{answer.row_count} rows exceeds max {spec['max_rows']}"
        return True, f"{expected_metric}, {answer.row_count} row(s)"

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
        # This has to be able to SEE the SQL. With include_sql off, result.sql
        # is None and the check certified a statement it never read.
        if not result.sql:
            raise SpecificationError(
                "type: no_pricing requires the run to capture SQL (include_sql)")
        if "wac" in result.sql.lower():
            return False, "SQL CONTAINS WAC"
        if not answer:
            return True, "no answer, so no pricing"
        # The headline carries the figure; checking only value_formatted missed it.
        text = _text_of(answer)
        if "$" in text:
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
        needles = [n.lower() for n in spec.get("note_contains", [])]

        if "answered_with_note" in allowed and not needles:
            # amb-01 asked for generic share, returned company BRAND share at
            # 71.79%, and passed -- because every answer covering the current
            # period carries an incomplete-period note, and any note counted.
            raise SpecificationError(
                "answered_with_note requires `note_contains:` naming what the "
                "note must actually address")

        if result.status in allowed and result.status != "answered":
            return True, result.status
        if "answered_with_note" in allowed and result.status == "answered":
            if not answer:
                return False, "answered with no answer body"
            joined = " ".join(list(answer.notes) + list(answer.warnings)).lower()
            hit = next((n for n in needles if n in joined), None)
            if hit is None:
                return False, (
                    "no note addressing the question "
                    f"(wanted one of {needles}; got {answer.notes + answer.warnings})"
                )
            return True, f"note addresses {hit!r}"
        if result.status in allowed:
            return True, result.status
        return False, f"status {result.status} not in {sorted(allowed)}"

    if kind == "sql":
        if result.status != "answered" or not answer:
            return False, f"status {result.status}"
        rows = reference(spec["sql"], principal)
        compare = spec.get("compare", "scalar")

        if compare == "scalar":
            expected = list(rows[0].values())[0] if rows else None
            actual = answer.table[0]["value"] if answer.table else None
            # close(None, None) is True, so an empty answer used to "match" an
            # empty oracle. Neither side may be empty: a reference that
            # produces nothing is a broken question, not a passing one.
            if expected is None:
                raise SpecificationError(
                    "reference SQL produced no value; the question cannot be scored")
            if actual is None:
                return False, f"expected {expected!r}, got no value"
            ok = close(expected, actual, tolerance)
            return ok, f"expected {expected!r}, got {actual!r}"

        if compare == "top_ids":
            expected_ids = [list(r.values())[0] for r in rows]
            if not expected_ids:
                raise SpecificationError(
                    "reference SQL produced no ids; the question cannot be scored")
            actual_ids = [r.get("dim0_id") for r in answer.table][: len(expected_ids)]
            if len(actual_ids) < len(expected_ids):
                return False, f"got {len(actual_ids)} ids, expected {len(expected_ids)}"
            # Compare as sets: equal values may tie in either order.
            if set(expected_ids) == set(actual_ids):
                return True, f"{len(expected_ids)} ids match"
            missing = set(expected_ids) - set(actual_ids)
            extra = set(actual_ids) - set(expected_ids)
            return False, f"missing {sorted(missing)[:5]}, unexpected {sorted(extra)[:5]}"

        if compare == "rows":
            expected_map = {str(r["label"]): r["value"] for r in rows}
            if not expected_map:
                raise SpecificationError(
                    "reference SQL produced no groups; the question cannot be scored")
            actual_map = {str(r.get("dim0")): r.get("value") for r in answer.table}
            # Both directions. Checking only that the expected groups are
            # present ignored groups the oracle never produced.
            unexpected = set(actual_map) - set(expected_map)
            if unexpected:
                return False, f"groups not in the reference: {sorted(unexpected)[:5]}"
            for label, value in expected_map.items():
                if label not in actual_map:
                    return False, f"missing group {label!r}"
                if not close(value, actual_map[label], tolerance):
                    return False, f"{label}: expected {value!r}, got {actual_map[label]!r}"
            return True, f"{len(expected_map)} group(s) match"

        raise SpecificationError(f"unknown comparison {compare!r}")

    # A typo used to become a question that simply never passed, which reads
    # as a system failure in the report.
    raise SpecificationError(f"unknown expectation type {kind!r}")


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
                "category": categorise(turn["expect"], result, ok),
                "plan": result.plan,
                "sql": result.sql,
                # The ANSWER is recorded too, not just the plan. Without it a
                # stored run cannot be re-judged after the judge is corrected:
                # when this judge was repaired, amb-01's live result could not
                # be rescored because its notes had never been written down,
                # and the reason string was all that survived.
                "answer": None if not result.answer else {
                    "headline": result.answer.headline,
                    "columns": result.answer.columns,
                    "rows": result.answer.table[:20],
                    "row_count": result.answer.row_count,
                    "truncated": result.answer.truncated,
                    "scope_note": result.answer.scope_note,
                    "period_note": result.answer.period_note,
                    "notes": result.answer.notes,
                    "warnings": result.answer.warnings,
                },
                "alternative": result.alternative,
                "message": result.message,
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
        "by_category": {
            key: sum(1 for r in results if r["category"] == key) for key in CATEGORIES
        },
        "results": results,
    }
    path = RUNS / f"{stamp}-{args.provider}.json"
    path.write_text(json.dumps(record, indent=2, default=str))

    counts = {key: sum(1 for r in results if r["category"] == key) for key in CATEGORIES}

    print(f"\n  {passed}/{total} behavioural checks passed"
          + (f", {failed} failed" if failed else ""))
    print("\n  What those checks demonstrate, separately:")
    for key, label in CATEGORIES.items():
        print(f"    {counts[key]:>3}  {label}")
    answerable = counts["answer"] + counts["wrong"] + counts["failure"]
    if answerable:
        print(f"\n  Question-answering: {counts['answer']}/{answerable} of the questions"
              " this system claims to be able to compute.")
    print(f"\n  {counts['unsupported']} check(s) passed by correctly declining or"
          " clarifying. That is right behaviour,\n  and it is NOT evidence that"
          " the requested figure can be computed.")
    print(f"\n  written to {path.relative_to(ROOT)}")
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
