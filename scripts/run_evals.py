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


class SpecificationError(ValueError):
    """The expectation itself is unusable, so the question cannot be scored.

    Raised rather than returned as a failure: a question that can never pass
    and a question that is written wrong are different problems, and only one
    of them is the system's fault.
    """


# ---------------------------------------------------------------------------
# Reference answers -- computed independently of the system under test
# ---------------------------------------------------------------------------

def reference(sql: str, principal) -> Any:
    """Run the hand-written oracle. A broken oracle is not a system failure.

    If the reference query itself does not execute, the question cannot be
    scored and the fault is in the question file. Reporting that as an
    incorrect answer blames the wrong component -- which happened on the first
    held-out run, where two oracles referenced a column that does not exist
    and were counted against the system.
    """
    from app.db import analytics_transaction

    try:
        with analytics_transaction(
            scope_kind=principal.scope_kind,
            scope_value=principal.scope_value,
            wac_authorized=principal.wac_authorized,
        ) as cur:
            cur.execute(sql)
            return cur.fetchall()
    except SpecificationError:
        raise
    except Exception as exc:
        raise SpecificationError(
            f"the reference SQL does not execute: {type(exc).__name__}: "
            f"{str(exc).splitlines()[0]}"
        ) from None


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
    """Bucket one outcome. Failures split by whether the system broke.

    A PASSED check is always one of answer / refusal / unsupported, and a
    FAILED one always wrong / failure, so the categories sum to the pass and
    fail totals. They used not to: a `plan` check whose request errored was
    judged a pass AND bucketed as an execution failure, and the report said
    38/38 passed beside a non-zero failure count.
    """
    if not ok:
        return "failure" if result.status == "error" else "wrong"
    if result.status == "error":
        raise SpecificationError(
            "a check passed although its request errored; an error is never a "
            "correct outcome, so the judge for this spec is wrong")
    if result.status == "denied":
        return "refusal"
    if result.status == "clarify":
        return "unsupported"
    # Answered, and accepted. If the expectation would also have accepted a
    # clarification or a refusal, this was an unsupported request handled
    # gracefully -- not a demonstration that the metric can be computed.
    if spec.get("type") == "any_of" and (
            {"clarify", "denied"} & set(spec.get("allowed", []))):
        return "unsupported"
    if spec.get("type") == "volume_alternative":
        return "refusal"
    return "answer"


def plan_mismatch(expected_plan: dict, actual: dict | None) -> str | None:
    """Why the plan differs from what the spec expects, or None if it
    matches. Interpretation only -- says nothing about execution."""
    actual = actual or {}
    for key, expected in expected_plan.items():
        got = actual.get(key)
        if isinstance(expected, dict):
            for sub, value in expected.items():
                if (got or {}).get(sub) != value:
                    return f"plan.{key}.{sub} = {(got or {}).get(sub)!r}, expected {value!r}"
        elif isinstance(expected, list):
            if list(got or []) != expected:
                return f"plan.{key} = {got!r}, expected {expected!r}"
        elif got != expected:
            return f"plan.{key} = {got!r}, expected {expected!r}"
    return None


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
                # Checking only the last column header let a currency answer
                # through whenever the column happened not to be labelled USD.
                # The figure is in the headline and the formatted cells, so
                # look everywhere a reader would.
                body = _text_of(answer)
                # columns can be empty on a scalar answer; indexing it blindly
                # crashed the judge rather than judging.
                last_column = (answer.columns[-1] if answer.columns else "") or ""
                if "$" in body or "usd" in last_column.lower():
                    return False, "returned currency while claiming to substitute volume"
                if result.sql and "wac" in result.sql.lower():
                    return False, "volume alternative compiled against the wac column"
                return True, "answered in volume, restriction disclosed"
            return False, "answered without disclosing the pricing restriction"
        return False, f"status {result.status}"

    if kind == "no_pricing":
        # An error is not a pass. A request that blew up discloses no pricing
        # in the same sense that a request never sent discloses none, and
        # counting it as a success turns every crash into evidence of
        # security.
        if result.status not in ("answered", "denied", "clarify"):
            return False, f"status {result.status}: nothing was actually checked"
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
        if mismatch := plan_mismatch(spec["plan"], result.plan):
            return False, mismatch
        # A matching plan is a correct INTERPRETATION. It is not an answer:
        # a compile failure carries its plan, so status="error" with no
        # answer used to pass as "plan matches". The plan-only score is
        # reported separately (plan_matched); the end-to-end check needs the
        # request to have done what the spec expects.
        want = spec.get("status", "answered")
        if result.status != want:
            return False, (f"plan matches, but the request ended {result.status!r}"
                           f" where {want!r} was expected")
        if want == "answered" and not result.answer:
            return False, "plan matches, but no answer was produced"
        return True, "plan matches and the request succeeded"

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
            # scope_note is displayed with every answer and is where scope
            # narrowing is disclosed ("Showing data for the Northeast region
            # only"). Searching only notes and warnings missed it, so a
            # correctly disclosed answer was judged undisclosed.
            #
            # Deliberately NOT the headline or the table: a needle appearing
            # in the figure itself is not a qualification of it.
            joined = " ".join(
                list(answer.notes) + list(answer.warnings)
                + [answer.scope_note or "", answer.period_note or ""]
            ).lower()
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
        # The population actually APPLIED. Since 30 September the server binds
        # the stored cohort to the query itself (the plan's account filter
        # holds only 200 ids), so the plan no longer carries it -- and what
        # matters was always what the query ran against, not what the plan
        # said. Falls back to the plan's filter for runs recorded earlier.
        applied = getattr(result, "applied_cohort", None)
        ids = list(applied[1]) if applied else (
            ((result.plan or {}).get("filters") or {}).get("account_ids") or [])
        if not ids:
            return False, "no cohort was applied to the follow-up"
        if (result.plan or {}).get("ranking"):
            return False, "frozen cohort was re-ranked"
        # "Frozen" means THESE accounts, not some accounts. Checking only that
        # the list was non-empty accepted an entirely unrelated population as
        # a correctly preserved cohort -- which is the one thing this
        # expectation exists to catch.
        previous_answer = getattr(previous, "answer", None)
        expected = [
            str(row.get("dim0_id")) for row in (previous_answer.table if previous_answer else [])
            if row.get("dim0_id") is not None
        ]
        if not expected:
            raise SpecificationError(
                "frozen_cohort needs a previous turn that returned identified rows")
        if set(map(str, ids)) != set(expected):
            missing = sorted(set(expected) - set(map(str, ids)))[:3]
            extra = sorted(set(map(str, ids)) - set(expected))[:3]
            return False, (
                f"cohort is not the previous turn's rows "
                f"(missing {missing}, unexpected {extra})"
            )
        return True, f"cohort frozen to the previous turn's {len(ids)} ids"
    return judge(spec, result, None, 1e-9)


# ---------------------------------------------------------------------------
# Spend, and which sets may be run
# ---------------------------------------------------------------------------

FROZEN = ROOT / "evals" / "frozen.json"


class Budget:
    """Tokens a live run may spend, checked BEFORE each question.

    A question can cost two model calls (the plan and one repair), and a call
    whose usage the provider did not report -- a timeout, a dropped
    connection -- may still have been billed. So: the next question runs only
    if two more calls at the ceiling would still fit, and an unreported call
    is charged AT the ceiling, never as zero. The ceilings are deliberately
    above what was measured (about 4,670 input / 160 output per question).
    """

    def __init__(self, max_input: int, max_output: int, *,
                 input_ceiling: int = 8_000, output_ceiling: int = 4_096):
        self.max_input, self.max_output = max_input, max_output
        self.input_ceiling, self.output_ceiling = input_ceiling, output_ceiling
        self.input = self.output = 0
        self.unreported_calls = 0

    def can_afford_another(self) -> bool:
        return (self.input + 2 * self.input_ceiling <= self.max_input
                and self.output + 2 * self.output_ceiling <= self.max_output)

    def charge(self, planning: dict[str, Any] | None) -> None:
        attempts = (planning or {}).get("attempts")
        if not attempts:
            # Planning failed before reporting what it did: the worst case.
            self.input += 2 * self.input_ceiling
            self.output += 2 * self.output_ceiling
            self.unreported_calls += 2
            return
        for attempt in attempts:
            usage = attempt.get("usage") or {}
            if usage.get("known"):
                self.input += usage.get("input_tokens") or 0
                self.output += usage.get("output_tokens") or 0
            else:
                self.input += self.input_ceiling
                self.output += self.output_ceiling
                self.unreported_calls += 1

    def as_dict(self) -> dict[str, Any]:
        return {"max_input_tokens": self.max_input, "max_output_tokens": self.max_output,
                "charged_input_tokens": self.input, "charged_output_tokens": self.output,
                "unreported_calls_charged_at_ceiling": self.unreported_calls,
                "input_ceiling_per_call": self.input_ceiling,
                "output_ceiling_per_call": self.output_ceiling}


def smoke_subset(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first question of each family: the cheapest run that still touches
    every kind of behaviour, to try before spending on whole sets."""
    seen, out = set(), []
    for q in questions:
        family = q.get("family") or q["id"]
        if family not in seen:
            seen.add(family)
            out.append(q)
    return out


def file_sha256(path: pathlib.Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runnable(path: pathlib.Path, spec: dict[str, Any]) -> tuple[bool, str]:
    """A set declares what it is. A holdout may run only as frozen -- the
    questions and expected answers exactly as recorded in evals/frozen.json
    before anyone saw the system's answers. Regression and spent sets run
    freely; their scores are never quoted as accuracy on unseen questions."""
    status = spec.get("status")
    if status in ("regression", "spent"):
        return True, status
    if status != "holdout":
        return False, f"{path.name} has no recognised status (regression | spent | holdout)"
    frozen = json.loads(FROZEN.read_text()) if FROZEN.exists() else {}
    entry = frozen.get(path.name)
    if entry is None:
        return False, (f"{path.name} is a holdout that has not been frozen; run "
                       f"scripts/freeze_holdout.py {path} before evaluating it")
    if entry["sha256"] != file_sha256(path):
        return False, (f"{path.name} changed after it was frozen on {entry['frozen_at']}; "
                       f"a changed holdout is a new set and needs a new name")
    return True, "holdout"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("offline", "bedrock"), default="offline")
    ap.add_argument(
        "--questions", default=str(QUESTIONS),
        help="question file (default the regression set; evals/holdout.yaml is "
             "the sealed held-out set, which is run ONCE)")
    ap.add_argument("--tolerance", type=float, default=1e-9)
    ap.add_argument("--family", help="run only one family")
    ap.add_argument("--id", dest="only", help="run only one question id")
    ap.add_argument("--smoke", action="store_true",
                    help="only the first question of each family")
    ap.add_argument("--max-input-tokens", type=int,
                    help="spend cap; REQUIRED with --provider bedrock")
    ap.add_argument("--max-output-tokens", type=int,
                    help="spend cap; REQUIRED with --provider bedrock")
    args = ap.parse_args()

    if args.provider == "bedrock":
        if not (args.max_input_tokens and args.max_output_tokens):
            print("A live run needs a budget: --max-input-tokens and --max-output-tokens.",
                  file=sys.stderr)
            return 2
        print("Running against AWS Bedrock. THIS COSTS MONEY.\n")
    budget = (Budget(args.max_input_tokens, args.max_output_tokens)
              if args.provider == "bedrock" else None)

    import os

    os.environ["PAC_LLM_PROVIDER"] = args.provider
    # A batch measurement, not a user: the per-user request limits exist to
    # protect the service from one person's browser, and would throttle a run
    # that asks dozens of questions as the same principal in seconds.
    for name in ("PAC_USER_REQUESTS_PER_MINUTE", "PAC_USER_REQUESTS_PER_HOUR",
                 "PAC_USER_CONCURRENT_RUNS"):
        os.environ.setdefault(name, "1000000")
    from app.config import get_settings

    get_settings.cache_clear()

    from app.auth.policy import principal_for_user_id
    from app.db import close_pools, owner_transaction
    from app.llm.planner import build_planner
    from app.pipeline import Pipeline

    questions_path = pathlib.Path(args.questions)
    spec = yaml.safe_load(questions_path.read_text())
    ok, set_status = runnable(questions_path, spec)
    if not ok:
        print(set_status, file=sys.stderr)
        return 2
    questions = spec["questions"]
    if args.smoke:
        questions = smoke_subset(questions)
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

    from app.llm.planner import PROMPT_VERSION
    from app.llm.prompt_fingerprint import prompt_fingerprint

    planner = build_planner()
    pipeline = Pipeline(planner)
    dataset = pipeline.current_dataset()

    from app.analytics.registry import get_registry

    results: list[dict[str, Any]] = []
    passed = failed = 0

    not_run: list[str] = []
    for item in questions:
        if budget is not None and not budget.can_afford_another():
            not_run.append(item["id"])
            continue
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

            # From the result of THIS request, not from planner state.
            planning = result.planning or {}
            if budget is not None:
                budget.charge(result.planning)
            usage = planning.get("usage") or {}
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
                # Interpretation, scored apart from execution. None when the
                # check is not about the plan.
                "plan_matched": (None if turn["expect"].get("type") != "plan"
                                 else plan_mismatch(turn["expect"]["plan"], result.plan) is None),
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
                "usage_known": usage.get("known"),
                "provider": planning.get("provider"),
                "planner_attempts": len(planning.get("attempts") or []),
                "planner_repaired": planning.get("repaired"),
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
        "question_set_status": set_status,
        "question_set_sha256": file_sha256(questions_path),
        "smoke_subset": args.smoke,
        "prompt_version": PROMPT_VERSION,
        "prompt_fingerprint": prompt_fingerprint(),
        "budget": budget.as_dict() if budget is not None else None,
        "not_run_budget_exhausted": not_run,
        "metric_version": get_registry().version,
        "policy_version": "1.0.0",
        "total": total, "passed": passed, "failed": failed,
        "by_category": {
            key: sum(1 for r in results if r["category"] == key) for key in CATEGORIES
        },
        "plan_extraction": {
            "checks": sum(1 for r in results if r["plan_matched"] is not None),
            "matched": sum(1 for r in results if r["plan_matched"]),
            "matched_and_executed": sum(
                1 for r in results if r["plan_matched"] and r["passed"]),
        },
        "results": results,
    }
    label = questions_path.stem
    path = RUNS / f"{stamp}-{args.provider}-{label}.json"
    path.write_text(json.dumps(record, indent=2, default=str))

    counts = {key: sum(1 for r in results if r["category"] == key) for key in CATEGORIES}

    # The totals and the categories are two views of the same checks. If they
    # disagree, one of them is wrong, and a report that prints both lets the
    # reader pick the flattering one.
    if passed != counts["answer"] + counts["refusal"] + counts["unsupported"] \
            or failed != counts["wrong"] + counts["failure"]:
        raise SpecificationError(
            f"totals do not reconcile: {passed} passed / {failed} failed vs "
            f"categories {counts}")

    print(f"\n  {passed}/{total} behavioural checks passed"
          + (f", {failed} failed" if failed else ""))
    print("\n  What those checks demonstrate, separately:")
    for key, label in CATEGORIES.items():
        print(f"    {counts[key]:>3}  {label}")
    answerable = counts["answer"] + counts["wrong"] + counts["failure"]
    if answerable:
        print(f"\n  Question-answering: {counts['answer']}/{answerable} of the questions"
              " this system claims to be able to compute.")
    plan_checks = [r for r in results if r["plan_matched"] is not None]
    if plan_checks:
        matched = [r for r in plan_checks if r["plan_matched"]]
        executed = [r for r in matched if r["passed"]]
        print(f"\n  Plan extraction, scored apart from execution: "
              f"{len(matched)}/{len(plan_checks)} plans matched; "
              f"{len(executed)} of those also ran to the expected outcome.")
    print(f"\n  {counts['unsupported']} check(s) passed by correctly declining or"
          " clarifying. That is right behaviour,\n  and it is NOT evidence that"
          " the requested figure can be computed.")
    if budget is not None:
        b = budget.as_dict()
        print(f"\n  Spend: {b['charged_input_tokens']:,} input / {b['charged_output_tokens']:,} "
              f"output tokens charged against {b['max_input_tokens']:,} / "
              f"{b['max_output_tokens']:,}; {b['unreported_calls_charged_at_ceiling']} "
              f"unreported call(s) charged at the ceiling.")
    if not_run:
        print(f"\n  STOPPED BY BUDGET: {len(not_run)} question(s) not run: {', '.join(not_run)}."
              "\n  The totals above cover only the questions that ran.")
    print(f"\n  written to {path.relative_to(ROOT)}")
    if args.provider == "offline":
        print(
            "\n  NOTE: the offline planner is a deterministic keyword matcher.\n"
            "  This run exercises the compiler, authorization, execution and\n"
            "  rendering layers. It is NOT a measurement of natural-language\n"
            "  accuracy and must not be reported as one."
        )
    close_pools()
    # A run the budget cut short is not a pass, however its questions did.
    return 0 if failed == 0 and not not_run else 1


if __name__ == "__main__":
    sys.exit(main())
