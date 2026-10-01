"""The admission gate: a fixed number of holders, a bounded queue served in
arrival order, and refusals that happen at once or within a stated wait.

Threads and real time, deliberately: the gate's job is concurrency.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.admission import FairGate, Overloaded


def hold(gate: FairGate, release: threading.Event, admitted: list, label: str,
         max_wait: float = 5.0, errors: list | None = None):
    def run():
        try:
            with gate.admitted(max_wait=max_wait):
                admitted.append(label)
                release.wait(5)
        except BaseException as exc:          # recorded for the assertion
            (errors if errors is not None else []).append((label, type(exc).__name__))
    t = threading.Thread(target=run)
    t.start()
    return t


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_no_more_than_the_slots_hold_at_once():
    gate, release, admitted = FairGate("t", slots=2, queue=5), threading.Event(), []
    threads = [hold(gate, release, admitted, str(i)) for i in range(4)]
    assert wait_until(lambda: gate.holders == 2 and gate.waiting == 2)
    assert len(admitted) == 2
    release.set()
    for t in threads:
        t.join(5)
    assert sorted(admitted) == ["0", "1", "2", "3"] and gate.holders == 0


def test_waiters_are_admitted_in_arrival_order():
    gate, admitted = FairGate("t", slots=1, queue=10), []
    first = threading.Event()
    blocker = hold(gate, first, admitted, "blocker")
    assert wait_until(lambda: gate.holders == 1)
    threads = []
    for i in range(5):
        ev = threading.Event()
        ev.set()                              # each leaves at once when admitted
        threads.append(hold(gate, ev, admitted, f"w{i}"))
        assert wait_until(lambda: gate.waiting == i + 1)
    first.set()
    for t in [blocker, *threads]:
        t.join(5)
    assert admitted == ["blocker", "w0", "w1", "w2", "w3", "w4"]


def test_a_full_queue_refuses_at_once():
    gate, release, admitted = FairGate("t", slots=1, queue=1), threading.Event(), []
    threads = [hold(gate, release, admitted, "a"), ]
    assert wait_until(lambda: gate.holders == 1)
    threads.append(hold(gate, release, admitted, "b"))
    assert wait_until(lambda: gate.waiting == 1)
    started = time.monotonic()
    with pytest.raises(Overloaded) as exc:
        with gate.admitted(max_wait=5):
            pass
    assert time.monotonic() - started < 0.1
    assert (exc.value.stage, exc.value.reason) == ("t", "queue_full")
    release.set()
    for t in threads:
        t.join(5)


def test_a_wait_that_runs_out_is_refused_and_leaves_the_queue():
    gate, release, admitted = FairGate("t", slots=1, queue=5), threading.Event(), []
    blocker = hold(gate, release, admitted, "blocker")
    assert wait_until(lambda: gate.holders == 1)
    started = time.monotonic()
    with pytest.raises(Overloaded) as exc:
        with gate.admitted(max_wait=0.3):
            pass
    assert 0.25 < time.monotonic() - started < 1.0
    assert exc.value.reason == "wait_timeout" and gate.waiting == 0
    release.set()
    blocker.join(5)


def test_a_cancelled_wait_leaves_the_queue_and_the_next_one_still_gets_in():
    gate, release, admitted = FairGate("t", slots=1, queue=5), threading.Event(), []
    blocker = hold(gate, release, admitted, "blocker")
    assert wait_until(lambda: gate.holders == 1)

    class Cancelled(Exception):
        pass

    def cancel():
        raise Cancelled()

    with pytest.raises(Cancelled):
        with gate.admitted(max_wait=5, should_stop=cancel):
            pass
    assert gate.waiting == 0
    done = threading.Event()
    done.set()                                # leaves as soon as admitted
    later = hold(gate, done, admitted, "later")
    release.set()
    blocker.join(5)
    later.join(5)
    assert admitted == ["blocker", "later"]


def test_a_slot_is_released_when_the_holder_fails():
    gate = FairGate("t", slots=1, queue=0)
    with pytest.raises(ValueError):
        with gate.admitted(max_wait=0):
            raise ValueError("query failed")
    with gate.admitted(max_wait=0):
        assert gate.holders == 1
    assert gate.holders == 0
