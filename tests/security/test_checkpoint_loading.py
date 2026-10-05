"""Tampered workflow storage must never act as executable instructions."""
import os
import subprocess
import sys

import ormsgpack
import pytest
from langgraph.checkpoint.serde.jsonplus import EXT_CONSTRUCTOR_SINGLE_ARG

from tests.security.test_graph_durability import ask, pending_thread, restart, twins
from tests.security.helpers import sign_in

pytestmark = pytest.mark.security


def constructor_blob():
    return ormsgpack.packb(ormsgpack.Ext(
        EXT_CONSTRUCTOR_SINGLE_ARG,
        ormsgpack.packb(("builtins", "print", "CHECKPOINT_CONSTRUCTOR_CALLED"))))


def test_runtime_saver_rejects_constructor_without_calling_it(monkeypatch):
    from app.graph import checkpointer
    monkeypatch.setattr("app.db.graph_pool", lambda: None)
    called = []
    monkeypatch.setattr("builtins.print", lambda *args: called.append(args))
    with pytest.raises(ValueError):
        checkpointer().serde.loads_typed(("msgpack", constructor_blob()))
    assert called == []


def test_fresh_worker_is_safe_even_when_environment_requests_permissive_loading():
    code = '''
from unittest.mock import patch
from app.graph import checkpointer
from tests.security.test_checkpoint_loading import constructor_blob
with patch("app.db.graph_pool", return_value=None):
    try:
        checkpointer().serde.loads_typed(("msgpack", constructor_blob()))
    except ValueError:
        print("REFUSED")
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env={**os.environ, "LANGGRAPH_STRICT_MSGPACK": "false"})
    assert result.returncode == 0, result.stderr
    assert "CHECKPOINT_CONSTRUCTOR_CALLED" not in result.stdout
    assert result.stdout.strip() == "REFUSED"


def test_unsupported_encodings_and_nested_constructors_are_refused(monkeypatch):
    from app.graph import checkpointer
    monkeypatch.setattr("app.db.graph_pool", lambda: None)
    serde = checkpointer().serde
    for kind, blob in [
        ("pickle", b"N."),
        ("json", b'{"lc":2,"type":"constructor","id":["builtins","print"],"args":["marker"]}'),
        ("msgpack", ormsgpack.packb({"chosen": ormsgpack.Ext(99, b"\xc0")})),
        ("msgpack", ormsgpack.packb({"nested": ormsgpack.Ext(
            EXT_CONSTRUCTOR_SINGLE_ARG,
            ormsgpack.packb(("builtins", "print", "CHECKPOINT_CONSTRUCTOR_CALLED")))})),
    ]:
        with pytest.raises(ValueError):
            serde.loads_typed((kind, blob))


def test_a_new_process_resumes_the_postgres_interrupt(client, make_identity, twins):
    user = make_identity("exec", can_view_wac=1)
    sign_in(client, user)
    first = ask(client, "What was the volume for Pactest Graph Twin last quarter?").json()
    assert first["status"] == "clarify"
    code = '''
import sys
from app.auth.policy import principal_for_user_id
from app.llm.planner import OfflinePlanner
from app.pipeline import Pipeline
from app.db import close_pools
result = Pipeline(OfflinePlanner()).ask(principal_for_user_id(sys.argv[1]),
    "the second one", conversation_id=sys.argv[2])
print(result.status)
close_pools()
'''
    result = subprocess.run([sys.executable, "-c", code, user.user_id,
                             first["conversation_id"]], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "answered", result.stdout


@pytest.mark.parametrize("payload", [constructor_blob(), b"\xc1", ormsgpack.packb(17)])
def test_unreadable_paused_state_is_refused_and_releases_run(
        client, make_identity, twins, monkeypatch, payload):
    from app.db import owner_transaction
    sign_in(client, make_identity("exec", can_view_wac=1))
    first = ask(client, "What was the volume for Pactest Graph Twin last quarter?").json()
    thread = pending_thread(first["conversation_id"])
    assert thread
    with owner_transaction() as cur:
        cur.execute("UPDATE app_graph.checkpoint_blobs SET type = 'msgpack', blob = %s "
                    "WHERE thread_id = %s AND channel = 'chosen'", (payload, thread))
        assert cur.rowcount > 0
    restart()
    called = []
    original_print = print
    def observe_print(*args, **kwargs):
        if args == ("CHECKPOINT_CONSTRUCTOR_CALLED",):
            called.append(args)
        else:
            original_print(*args, **kwargs)
    monkeypatch.setattr("builtins.print", observe_print)
    response = ask(client, "the second one", first["conversation_id"], key="bad-checkpoint")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error" and "saved" in body["message"].lower()
    assert not body.get("answer")
    assert called == []
    with owner_transaction() as cur:
        cur.execute("SELECT status FROM app_conv.runs WHERE run_id = %s", (body["run_id"],))
        assert cur.fetchone()["status"] == "failed"
    # A new question is possible immediately; a damaged load cannot leave a lease busy.
    assert ask(client, "Total volume this quarter", first["conversation_id"]).json()["status"] == "answered"
