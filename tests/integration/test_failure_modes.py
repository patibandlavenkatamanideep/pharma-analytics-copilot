"""When a dependency fails, the request ends quickly, says what happened in
words a user can act on, and leaves nothing half-done.

Provider failures use the SDK's own exception classes through a fake
transport -- no network. Database failures are real: a statement timeout
against the full dataset, a pool with no free connection, a pool that cannot
connect, a checkpoint write that fails.
"""

from __future__ import annotations

import secrets
import threading
import time

import anthropic
import httpx2
import psycopg
import pytest
from psycopg_pool import ConnectionPool

from tests.unit.test_live_adapter_contract import (
    FakeResponse, FakeUsage, make_planner, valid_plan_block,
)

QUESTION = "top 5 accounts by paid pack units last quarter"


def audit_status(request_id: str) -> str | None:
    from app.db import auth_transaction
    with auth_transaction() as cur:
        cur.execute("SELECT status FROM app_meta.query_audit WHERE request_id = %s",
                    (request_id,))
        row = cur.fetchone()
    return row["status"] if row else None


def pipeline_with_transport(replies):
    from app.pipeline import Pipeline
    planner = make_planner(replies)
    return Pipeline(planner), planner


REQUEST = httpx2.Request("POST", "https://bedrock-runtime.invalid/model/invoke")


# -- the model provider ------------------------------------------------------------

@pytest.mark.parametrize("failure", [
    anthropic.APITimeoutError(request=REQUEST),
    anthropic.RateLimitError("rate limited", response=httpx2.Response(429, request=REQUEST),
                             body=None),
    anthropic.InternalServerError("overloaded", response=httpx2.Response(529, request=REQUEST),
                                  body=None),
], ids=["timeout", "rate-limited", "overloaded"])
def test_a_provider_failure_is_reported_as_unavailable_not_as_a_bad_question(exec_user, failure):
    pipe, planner = pipeline_with_transport([failure])
    started = time.perf_counter()
    result = pipe.ask(exec_user, QUESTION)
    assert result.status == "error"
    assert "busy or unreachable" in result.message
    assert "rephrase" not in result.message and "interpret" not in result.message
    assert len(planner._client.messages.requests) == 1     # no "repair" of nothing
    assert audit_status(result.request_id) == "planner_unavailable"
    assert time.perf_counter() - started < 5


class Slow:
    def __init__(self, seconds):
        self.seconds = seconds
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        time.sleep(self.seconds)
        return FakeResponse([valid_plan_block()], FakeUsage(10, 1))


def test_no_model_call_starts_without_time_to_finish(exec_user, monkeypatch):
    pipe, planner = pipeline_with_transport([])
    planner._client.messages = Slow(0)
    monkeypatch.setattr(pipe.settings, "request_deadline_seconds", 1)
    started = time.perf_counter()
    result = pipe.ask(exec_user, QUESTION)
    assert result.status == "error" and "took too long" in result.message
    assert planner._client.messages.requests == []
    assert audit_status(result.request_id) == "deadline_exceeded"
    assert time.perf_counter() - started < 2


def test_a_slow_model_cannot_carry_a_request_past_its_deadline(exec_user, monkeypatch):
    """The model answers, but after the request's budget: the plan is not
    executed. (A real client is also given the remaining budget as its
    timeout -- recorded below -- so it would have stopped waiting itself.)"""
    pipe, planner = pipeline_with_transport([])
    planner._client.messages = Slow(3.6)
    monkeypatch.setattr(pipe.settings, "request_deadline_seconds", 3.5)
    started = time.perf_counter()
    result = pipe.ask(exec_user, QUESTION)
    elapsed = time.perf_counter() - started
    assert result.status == "error" and "took too long" in result.message
    assert audit_status(result.request_id) == "deadline_exceeded"
    assert planner._client.options[0]["timeout"] <= 3.5
    assert elapsed < 6, elapsed


# -- the database ------------------------------------------------------------------

def test_a_statement_timeout_ends_the_query_and_says_how_to_narrow_it(pipeline, exec_user,
                                                                       monkeypatch):
    from app.config import get_settings

    question = "paid pack units by facility and month, all time"
    pipeline.ask(exec_user, question)         # vocabularies cached; only the query is timed
    monkeypatch.setattr(get_settings(), "statement_timeout_ms", 1)
    started = time.perf_counter()
    result = pipeline.ask(exec_user, question)
    assert result.status == "error"
    assert "took too long" in result.message and "Narrowing" in result.message
    assert audit_status(result.request_id) == "db_error"
    assert time.perf_counter() - started < 5


def _patched_pool(monkeypatch, role: str, replacement):
    import app.db as db

    original = db.get_pool
    monkeypatch.setattr(db, "get_pool",
                        lambda r: replacement() if r == role else original(r))


def test_an_exhausted_pool_fails_the_request_in_bounded_time(pipeline, exec_user, monkeypatch):
    from app.config import get_settings

    pipeline.ask(exec_user, QUESTION)         # vocabularies cached; only the query needs a connection
    tiny = ConnectionPool(get_settings().dsn("exec"), min_size=1, max_size=1, timeout=0.5,
                          open=True)
    held = tiny.getconn()
    try:
        _patched_pool(monkeypatch, "exec", lambda: tiny)
        started = time.perf_counter()
        result = pipeline.ask(exec_user, QUESTION)
        elapsed = time.perf_counter() - started
    finally:
        tiny.putconn(held)
        tiny.close()
    assert result.status == "error" and "temporarily unavailable" in result.message
    assert audit_status(result.request_id) == "db_error"
    assert 0.4 < elapsed < 3, elapsed


def test_an_unreachable_database_fails_the_request_in_bounded_time(pipeline, exec_user,
                                                                   monkeypatch):
    pipeline.ask(exec_user, QUESTION)

    def unreachable():
        raise psycopg.OperationalError("connection refused")

    _patched_pool(monkeypatch, "exec", unreachable)
    result = pipeline.ask(exec_user, QUESTION)
    assert result.status == "error" and "temporarily unavailable" in result.message
    assert "refused" not in result.message


# -- workflow state ------------------------------------------------------------------

def test_a_failed_checkpoint_write_leaves_the_request_retryable_and_recorded_once(
        exec_user, monkeypatch):
    from app.db import auth_transaction
    from app.llm.planner import OfflinePlanner
    from app.pipeline import Pipeline

    pipe = Pipeline(OfflinePlanner())
    saver = pipe.graph.checkpointer
    real_put = type(saver).put
    calls = {"n": 0}

    def flaky_put(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("checkpoint store unavailable")
        return real_put(self, *args, **kwargs)

    monkeypatch.setattr(type(saver), "put", flaky_put)
    key = f"failure-{secrets.token_hex(6)}"
    with pytest.raises(psycopg.OperationalError):
        pipe.ask(exec_user, QUESTION, idempotency_key=key)

    retried = pipe.ask(exec_user, QUESTION, idempotency_key=key)
    assert retried.status == "answered"
    with auth_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM app_conv.turns WHERE conversation_id = %s",
                    (retried.conversation_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT status FROM app_conv.runs WHERE owner_user_id = %s "
                    "AND idempotency_key = %s", (exec_user.user_id, key))
        assert [r["status"] for r in cur.fetchall()] == ["succeeded"]


def test_concurrent_requests_under_pool_pressure_all_finish(pipeline, ram_user, director_user):
    """Twelve requests against pools sized for fewer: some wait, none hang,
    none fail with an internal error."""
    results, errors = [], []

    def one(user):
        try:
            results.append(pipeline.ask(user, QUESTION).status)
        except Exception as exc:          # pragma: no cover - the assertion reports it
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=one, args=(u,))
               for u in [ram_user, director_user] * 6]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert results.count("answered") == 12, results
    assert time.perf_counter() - started < 60


def test_an_exhausted_evaluation_budget_makes_no_model_call(exec_user):
    """A metered pipeline (evaluation only) whose spend cannot cover one
    more call asks the model nothing, says so, and records why."""
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "run_evals", pathlib.Path(__file__).resolve().parents[2] / "scripts" / "run_evals.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)

    pipe, planner = pipeline_with_transport([FakeResponse([valid_plan_block()], FakeUsage(10, 1))])
    pipe.spend = ev.Budget(1_000, 1_000)           # below any request's bound
    result = pipe.ask(exec_user, QUESTION)
    assert result.status == "error" and "spend limit" in result.message
    assert planner._client.messages.requests == []
    assert audit_status(result.request_id) == "budget_exhausted"


# ---------------------------------------------------------------------------
# The website's model allowance: every user, worker, replica and retry
# ---------------------------------------------------------------------------

def reset_allowance() -> None:
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("UPDATE app_meta.model_allowance SET committed_microusd = 0, calls = 0, "
                    "refused = 0, bound_violations = 0, unreported_calls = 0 "
                    "WHERE allowance_id = 'serving'")


def allowance_row() -> dict:
    from app.db import owner_transaction
    with owner_transaction() as cur:
        cur.execute("SELECT committed_microusd, calls, refused, bound_violations, unreported_calls "
                    "FROM app_meta.model_allowance WHERE allowance_id = 'serving'")
        return dict(cur.fetchone())


@pytest.fixture
def allowance(monkeypatch):
    """Configure PAC_LLM_SPEND_LIMIT_USD (with the rates that price it) for
    the pipelines built in one test, start from an untouched allowance, and
    restore both afterwards."""
    from app.config import get_settings

    def configure(limit_usd: str, input_rate: str | None = "5.5", output_rate: str | None = "27.5"):
        monkeypatch.setenv("PAC_LLM_SPEND_LIMIT_USD", limit_usd)
        for name, rate in (("PAC_LLM_INPUT_USD_PER_MTOK", input_rate),
                           ("PAC_LLM_OUTPUT_USD_PER_MTOK", output_rate)):
            if rate is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, rate)
        get_settings.cache_clear()

    reset_allowance()
    yield configure
    monkeypatch.undo()
    get_settings.cache_clear()
    reset_allowance()


def test_a_configured_allowance_limits_what_the_website_spends(exec_user, allowance):
    """An allowance below one call's bound: a question through the serving
    pipeline asks the model nothing and says why. An evaluation script's token
    cap does not reach this path; only the pipeline's own meter does."""
    allowance("0.0001")
    pipe, planner = pipeline_with_transport([FakeResponse([valid_plan_block()], FakeUsage(10, 1))])
    result = pipe.ask(exec_user, QUESTION)
    assert planner._client.messages.requests == []
    assert result.status == "error" and "spend limit" in result.message



def test_the_allowance_is_charged_what_was_billed_not_what_was_reserved(exec_user, allowance):
    allowance("1.00")
    pipe, planner = pipeline_with_transport([FakeResponse([valid_plan_block()], FakeUsage(10, 1))])
    assert pipe.ask(exec_user, QUESTION).status == "answered"
    row = allowance_row()
    # 10 input tokens at $5.50 and 1 output token at $27.50 per million.
    assert row["committed_microusd"] == 83 and row["calls"] == 1, row


def test_usage_the_provider_did_not_report_stays_charged_at_its_reservation(allowance):
    from app.llm.allowance import SharedAllowance

    allowance("1.00")
    meter = SharedAllowance(1.00, 5.5, 27.5)
    assert meter.reserve(20_000, 4_096)
    meter.record_call(FakeUsage(None, None), (20_000, 4_096))
    row = allowance_row()
    assert row["committed_microusd"] == meter.micro_usd(20_000, 4_096)
    assert row["unreported_calls"] == 1


def test_workers_cannot_take_the_last_of_the_allowance_together(allowance):
    """Ten concurrent reservations, each a separate meter as separate
    processes would hold, against an allowance with room for exactly three."""
    from app.llm.allowance import SharedAllowance

    allowance("1.00")
    cost = SharedAllowance(1, 5.5, 27.5).micro_usd(20_000, 4_096)
    limit = 3 * cost / 1_000_000
    granted, lock = [], threading.Lock()

    def worker():
        ok = SharedAllowance(limit, 5.5, 27.5).reserve(20_000, 4_096)
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    row = allowance_row()
    assert granted.count(True) == 3 and granted.count(False) == 7, granted
    assert row["committed_microusd"] == 3 * cost and row["refused"] == 7, row


def test_a_call_billed_beyond_its_bound_stops_every_further_call(allowance):
    from app.llm.allowance import SharedAllowance

    allowance("10.00")
    meter = SharedAllowance(10.00, 5.5, 27.5)
    assert meter.reserve(1_000, 100)
    meter.record_call(FakeUsage(5_000, 100), (1_000, 100))
    assert meter.violated
    assert allowance_row()["bound_violations"] == 1
    # Another process's meter: no further call anywhere.
    assert not SharedAllowance(10.00, 5.5, 27.5).reserve(10, 1)


def test_a_call_never_sent_returns_its_reservation(allowance):
    from app.llm.allowance import SharedAllowance

    allowance("1.00")
    meter = SharedAllowance(1.00, 5.5, 27.5)
    assert meter.reserve(20_000, 4_096)
    meter.release((20_000, 4_096))
    row = allowance_row()
    assert row["committed_microusd"] == 0 and row["calls"] == 0, row


def test_an_allowance_without_rates_refuses_every_call(exec_user, allowance):
    allowance("100.00", input_rate=None, output_rate=None)
    pipe, planner = pipeline_with_transport([FakeResponse([valid_plan_block()], FakeUsage(10, 1))])
    result = pipe.ask(exec_user, QUESTION)
    assert planner._client.messages.requests == []
    assert result.status == "error" and "spend limit" in result.message
