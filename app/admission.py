"""Admission control: how much work one worker accepts at once, and what
happens to the rest.

The load profile found the boundary (docs/CAPACITY.md). Throughput peaks at
about 8 concurrent analytical queries per replica; beyond that, queries
queue for connections and contend for the database, until the expensive
ones hit the 5 s statement timeout -- 5% of answers at 32 clients. A
timeout after waiting is the worst way to say no: the user waited, the
database worked, and the answer is still "try again".

So work is admitted in two places, and refused early and predictably:

* **Requests.** At most `admission_max_inflight_requests` questions are in
  flight per worker. Beyond that, POST /api/ask is answered at once with
  503 `overloaded` and Retry-After, before its body is read. Without this,
  requests queue without bound in the server's thread pool.
* **Queries.** At most `admission_query_slots` analytical queries run at
  once per worker, with at most `admission_query_queue` waiting, served in
  arrival order. A query waits at most `admission_query_wait_seconds`, and
  never beyond its request's deadline. A cancel stops the wait. A full queue
  or a wait that runs out is 503 `overloaded`. The run is closed as failed
  and its checkpoint kept, so a retry with the same idempotency key resumes
  after planning instead of paying for the model call again.

Fairness: one user can occupy at most `user_concurrent_runs` (2) places,
because that limit is counted in the database across every worker; the
queue is first-come-first-served, so no one is starved by later arrivals.

Per worker, deliberately: the limits are local and immediate, with no
coordination round-trip. Across a deployment the totals are per-worker
limits x workers x replicas, which is how they are sized
(docs/RUNBOOK.md §9).
"""

from __future__ import annotations

import collections
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from app import telemetry


class Overloaded(RuntimeError):
    """Refused for load. Never a statement about the request itself."""

    def __init__(self, stage: str, reason: str, retry_after: int):
        super().__init__(f"{stage} admission refused: {reason}")
        self.stage, self.reason, self.retry_after = stage, reason, retry_after


class FairGate:
    """At most `slots` holders; at most `queue` waiting, admitted in arrival
    order. Thread-safe; one per worker process."""

    def __init__(self, name: str, slots: int, queue: int, retry_after: int = 2):
        self.name, self.slots, self.queue, self.retry_after = name, slots, queue, retry_after
        self._lock = threading.Condition()
        self._holders = 0
        self._waiting: collections.deque[object] = collections.deque()

    @property
    def holders(self) -> int:
        return self._holders

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    def _refuse(self, reason: str) -> Overloaded:
        telemetry.count("pac.admission.refused", stage=self.name, reason=reason)
        return Overloaded(self.name, reason, self.retry_after)

    @contextmanager
    def admitted(self, *, max_wait: float,
                 should_stop: Callable[[], None] | None = None,
                 poll: float = 0.25) -> Iterator[float]:
        """Hold a slot for the duration. Yields the seconds spent waiting.

        `should_stop` is called between waits, outside the gate's lock, and
        may raise (a cancel, a deadline) to abandon the wait.
        """
        started = time.monotonic()
        ticket = object()
        with self._lock:
            if self._holders < self.slots and not self._waiting:
                self._holders += 1
                ticket = None
            elif len(self._waiting) >= self.queue:
                raise self._refuse("queue_full")
            else:
                self._waiting.append(ticket)
        try:
            while ticket is not None:
                with self._lock:
                    if self._waiting[0] is ticket and self._holders < self.slots:
                        self._waiting.popleft()
                        self._holders += 1
                        ticket = None
                        self._lock.notify_all()
                        break
                    remaining = max_wait - (time.monotonic() - started)
                    if remaining <= 0:
                        raise self._refuse("wait_timeout")
                    self._lock.wait(min(poll, remaining))
                if should_stop is not None:
                    should_stop()
        except BaseException:
            with self._lock:
                if ticket is not None and ticket in self._waiting:
                    self._waiting.remove(ticket)
                    self._lock.notify_all()
            raise
        waited = time.monotonic() - started
        telemetry.observe("pac.admission.wait", waited * 1000, stage=self.name)
        try:
            yield waited
        finally:
            with self._lock:
                self._holders -= 1
                self._lock.notify_all()


_gates: dict[str, FairGate] = {}
_gates_lock = threading.Lock()


def query_gate() -> FairGate:
    """This worker's gate for analytical queries, sized from settings."""
    from app.config import get_settings

    with _gates_lock:
        if "query" not in _gates:
            s = get_settings()
            _gates["query"] = FairGate("query", s.admission_query_slots,
                                       s.admission_query_queue)
        return _gates["query"]


def reset() -> None:
    """Forget the gates, so the next use reads current settings (tests)."""
    with _gates_lock:
        _gates.clear()
