"""One row per request: the lease, the idempotency record and the outcome.

Review finding 4. A conversation turn goes through read -> plan -> execute ->
record, and only the last step was serialised. Two requests in one
conversation could both plan from the same previous turn; a request retried
after a network failure was answered and recorded twice.

A run is the unit that fixes both:

* **Lease.** At most one live run per conversation, enforced by a partial
  unique index. A second request while one is live is refused as BUSY rather
  than planned against state that is about to change. A lease has an expiry,
  so a crashed worker cannot wedge a conversation: the next request marks the
  stale run abandoned and proceeds.
* **Idempotency.** A client key, scoped to owner and conversation, bound to a
  hash of the canonical request. Same key and same request: the stored
  outcome is replayed, not recomputed -- after checking the caller's CURRENT
  access still equals the access it was computed under, because an answer
  headline can carry WAC. Same key, different request: a conflict, because a
  key that could stand for two requests identifies neither.
* **Outcome.** The payload the client received, stored in the same
  transaction as the turn, so "answered and recorded" is one fact and a
  retry after a lost response gets the committed answer back. Only a
  committed outcome replays: a failed, conflicted or abandoned run is run
  again under the same key.

No transaction is held across planning or execution. Acquiring a run and
finishing it are two short transactions; between them the revision check in
state.finalise is what notices that the conversation moved.

What this does not claim: exactly-once model invocation. A retry of a run
that was abandoned mid-flight plans again. What is guaranteed is at most one
COMMITTED outcome per key.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

import psycopg

from app.auth.policy import Principal
from app.db import auth_transaction

#: Bumped when the meaning of the run contract changes.
RUN_CONTRACT_VERSION = "1.0.0"


class RunBusy(Exception):
    """Another run holds this conversation's lease."""

    def __init__(self, run_id: str, *, same_request: bool = False):
        self.run_id = run_id
        self.same_request = same_request
        super().__init__(
            "That request is still being answered." if same_request else
            "This conversation is already answering another question.")


class QuotaExceeded(Exception):
    """This user is asking faster, or more at once, than allowed."""

    def __init__(self, message: str, retry_after: int):
        self.retry_after = retry_after
        super().__init__(message)


class Cancelled(Exception):
    """The owner cancelled the run; it stops at the next step boundary."""


class IdempotencyConflict(Exception):
    """The key was used for a different request."""


class ReplayUnavailable(Exception):
    """A stored outcome exists, but the caller's access has changed since."""


@dataclass(frozen=True)
class Run:
    run_id: str
    conversation_id: str
    base_revision: int
    idempotency_key: str | None
    payload_hash: str
    #: A stored outcome to return instead of running again.
    replay: dict[str, Any] | None = None


def payload_hash(question: str, conversation_id: str | None, include_sql: bool) -> str:
    """The canonical request. Whitespace inside the question is collapsed --
    a retry that re-wraps a line is the same question -- but case is kept:
    it can change which entity is meant."""
    canonical = json.dumps({
        "question": " ".join((question or "").split()),
        "conversation_id": conversation_id,
        "include_sql": bool(include_sql),
        "contract": RUN_CONTRACT_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def find(principal: Principal, idempotency_key: str) -> dict[str, Any] | None:
    """The retained run this key names for this user, or None."""
    with auth_transaction() as cur:
        cur.execute(
            "SELECT run_id, conversation_id, payload_hash, status "
            "FROM app_conv.runs WHERE owner_user_id = %s AND idempotency_key = %s "
            "  AND expires_at > now()",
            (principal.user_id, idempotency_key))
        row = cur.fetchone()
    return dict(row) if row else None


def _new_run_id() -> str:
    return "r_" + secrets.token_urlsafe(12)


def acquire(
    principal: Principal,
    conversation_id: str,
    *,
    revision: int,
    request_hash: str,
    idempotency_key: str | None = None,
    lease_seconds: int = 120,
    retention_seconds: int = 86_400,
    limits: tuple[int, int, int] | None = None,
) -> Run:
    """Start a run, or return the stored outcome of the one this key names.

    Raises RunBusy, IdempotencyConflict or ReplayUnavailable.
    """
    try:
        with auth_transaction() as cur:
            # Serialise acquisition per conversation. Held for this short
            # transaction only -- never across planning.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (conversation_id,))

            if idempotency_key:
                cur.execute(
                    "SELECT run_id, payload_hash, status, outcome, scope_fingerprint, "
                    "       lease_expires_at > now() AS live, expires_at > now() AS retained "
                    "FROM app_conv.runs WHERE owner_user_id = %s AND idempotency_key = %s",
                    (principal.user_id, idempotency_key))
                prior = cur.fetchone()
                if prior is not None and not prior["retained"]:
                    # Past retention the key is free again; the old row goes.
                    cur.execute("DELETE FROM app_conv.runs WHERE run_id = %s",
                                (prior["run_id"],))
                    prior = None
                if prior is not None:
                    if prior["payload_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "That request key was already used for a different question.")
                    if prior["status"] == "running" and prior["live"]:
                        raise RunBusy(prior["run_id"], same_request=True)
                    if prior["status"] == "succeeded":
                        if prior["scope_fingerprint"] != principal.fingerprint():
                            raise ReplayUnavailable(
                                "Your access has changed since that request, so its "
                                "result is not shown again. Ask again to get an "
                                "answer under your current access.")
                        return Run(run_id=prior["run_id"], conversation_id=conversation_id,
                                   base_revision=revision, idempotency_key=idempotency_key,
                                   payload_hash=request_hash, replay=prior["outcome"])
                    # Only a COMMITTED outcome is replayed. A run that failed
                    # (a timeout, a provider error), lost a revision race, or
                    # died mid-flight committed nothing, and pinning that to
                    # the key would make a transient failure permanent for
                    # every retry. Reclaim the same row and run again, so the
                    # key still names exactly one run.
                    _reclaim_stale(cur, conversation_id)
                    _refuse_if_busy(cur, conversation_id)
                    cur.execute(
                        "UPDATE app_conv.runs SET status = 'running', base_revision = %s, "
                        "  scope_fingerprint = %s, outcome = NULL, turn_seq = NULL, "
                        "  finished_at = NULL, "
                        "  lease_expires_at = now() + make_interval(secs => %s) "
                        "WHERE run_id = %s",
                        (revision, principal.fingerprint(), lease_seconds, prior["run_id"]))
                    return Run(run_id=prior["run_id"], conversation_id=conversation_id,
                               base_revision=revision, idempotency_key=idempotency_key,
                               payload_hash=request_hash)

            _reclaim_stale(cur, conversation_id)
            _refuse_if_busy(cur, conversation_id)
            if limits is not None:
                _enforce_limits(cur, principal.user_id, *limits)
            run_id = _new_run_id()
            cur.execute(
                "INSERT INTO app_conv.runs (run_id, conversation_id, owner_user_id, "
                "  idempotency_key, payload_hash, scope_fingerprint, base_revision, "
                "  status, lease_expires_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', "
                "  now() + make_interval(secs => %s), now() + make_interval(secs => %s))",
                (run_id, conversation_id, principal.user_id, idempotency_key,
                 request_hash, principal.fingerprint(), revision,
                 lease_seconds, retention_seconds))
            return Run(run_id=run_id, conversation_id=conversation_id,
                       base_revision=revision, idempotency_key=idempotency_key,
                       payload_hash=request_hash)
    except psycopg.errors.UniqueViolation:
        # Lost a race the advisory lock should have prevented -- another
        # process without it, or a key reused concurrently. Either way the
        # conversation is busy; nothing was written.
        raise RunBusy("unknown") from None


def _enforce_limits(cur, user_id: str, per_minute: int, per_hour: int,
                    concurrent: int) -> None:
    """Per-user rate and concurrency, from the runs table.

    In the database rather than in process memory, so the limits hold across
    every worker and replica; serialised per user so two requests racing for
    the last slot cannot both take it. A replayed request never reaches
    here -- returning a stored answer costs nothing worth limiting.
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('quota:' || %s))", (user_id,))
    cur.execute(
        "SELECT count(*) FILTER (WHERE created_at > now() - interval '1 minute') AS minute, "
        "       count(*) FILTER (WHERE created_at > now() - interval '1 hour') AS hour, "
        "       count(*) FILTER (WHERE status = 'running' AND lease_expires_at > now()) AS live "
        "FROM app_conv.runs WHERE owner_user_id = %s "
        "  AND (created_at > now() - interval '1 hour' "
        "       OR (status = 'running' AND lease_expires_at > now()))",
        (user_id,))
    row = cur.fetchone()
    if row["live"] >= concurrent:
        raise QuotaExceeded(
            f"You already have {row['live']} questions being answered. Wait for one "
            f"to finish before asking another.", retry_after=5)
    if row["minute"] >= per_minute:
        raise QuotaExceeded("Too many questions in the last minute. Try again shortly.",
                            retry_after=60)
    if row["hour"] >= per_hour:
        raise QuotaExceeded("Too many questions in the last hour. Try again later.",
                            retry_after=900)


def request_cancel(principal: Principal, *, run_id: str | None = None,
                   idempotency_key: str | None = None) -> bool:
    """Ask a running run to stop. Owner only; True if a running run was found.

    By idempotency key as well as by run id, because a client cancelling an
    in-flight request does not yet have the run id -- it arrives with the
    response.
    """
    with auth_transaction() as cur:
        cur.execute(
            "UPDATE app_conv.runs SET cancel_requested_at = now() "
            "WHERE owner_user_id = %s AND status = 'running' "
            "  AND (run_id = %s OR (idempotency_key IS NOT NULL AND idempotency_key = %s))",
            (principal.user_id, run_id, idempotency_key))
        return cur.rowcount > 0


def cancel_requested(run_id: str) -> bool:
    with auth_transaction() as cur:
        cur.execute("SELECT cancel_requested_at IS NOT NULL AS c FROM app_conv.runs "
                    "WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return bool(row and row["c"])


def _reclaim_stale(cur, conversation_id: str) -> None:
    """A lease that expired belongs to a run that died. Mark it, so the
    one-running-run index no longer counts it."""
    cur.execute(
        "UPDATE app_conv.runs SET status = 'abandoned', finished_at = now() "
        "WHERE conversation_id = %s AND status = 'running' AND lease_expires_at <= now()",
        (conversation_id,))


def _refuse_if_busy(cur, conversation_id: str) -> None:
    cur.execute(
        "SELECT run_id FROM app_conv.runs WHERE conversation_id = %s AND status = 'running'",
        (conversation_id,))
    if live := cur.fetchone():
        raise RunBusy(live["run_id"])


def fail(run: Run, outcome: dict[str, Any] | None, *, status: str = "failed") -> None:
    """Close a run that did not commit a turn. Best effort: if this write
    fails the lease simply expires."""
    try:
        with auth_transaction() as cur:
            cur.execute(
                "UPDATE app_conv.runs SET status = %s, outcome = %s::jsonb, "
                "  finished_at = now() WHERE run_id = %s "
                "  AND status IN ('running', 'failed')",
                (status, json.dumps(outcome, default=str) if outcome else None, run.run_id))
    except Exception:                                            # pragma: no cover
        pass


def run_status(principal: Principal, run_id: str) -> dict[str, Any] | None:
    """A run's state, for its owner only. None for anyone else -- the same
    answer as for a run that does not exist."""
    with auth_transaction() as cur:
        cur.execute(
            "SELECT run_id, conversation_id, status, turn_seq, created_at, finished_at, "
            "       scope_fingerprint "
            "FROM app_conv.runs WHERE run_id = %s AND owner_user_id = %s",
            (run_id, principal.user_id))
        row = cur.fetchone()
    if row is None or row["scope_fingerprint"] != principal.fingerprint():
        return None
    return {k: row[k] for k in ("run_id", "conversation_id", "status", "turn_seq",
                                "created_at", "finished_at")}
