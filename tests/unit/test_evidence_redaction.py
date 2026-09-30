"""A secret must not be able to reach an evidence record.

Evidence records are committed. A credential that lands in one is published
to everyone who can read the repository, and rewriting history does not
un-publish it. So the property under test is not "the redactor has a rule for
this case" -- it is **a known string never appears in the file that was
written**.

Every test plants a unique synthetic sentinel, runs the real recorder, and
greps the resulting bytes. The sentinel is generated per run and is not a
credential to anything, so a failing assertion prints a random token rather
than a secret. Three shapes are covered, because they fail differently:

* an **environment override** -- a name/value pair the recorder controls,
  protected by an allowlist;
* a **connection string** in ordinary output -- no name to match on, only a
  shape;
* an **exception** raised by the child -- text the recorder never chose,
  arriving through a traceback.

The recorder previously matched variable NAMES against a pattern of
secret-sounding words. ``PAC_EVALUATOR_LOGIN``, ``PGPASSFILE`` and
``BEDROCK_BEARER`` are all credentials and none of them matched, which is the
defect these tests exist to hold closed.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
RECORDER = ROOT / "scripts" / "record_evidence.py"


def _load_recorder():
    """scripts/ is not a package; load the module by path."""
    spec = importlib.util.spec_from_file_location("record_evidence", RECORDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = _load_recorder()


@pytest.fixture
def sentinel() -> str:
    """A unique, synthetic, valueless string standing in for a credential."""
    return f"SENTINEL{uuid.uuid4().hex}"


def run_recorder(tmp_path: pathlib.Path, *args: str,
                 child: str | None = None) -> tuple[str, str]:
    """Run the recorder for real. Returns (file contents, recorder stdout)."""
    out = tmp_path / "record.json"
    argv = [sys.executable, str(RECORDER), "--out", str(out), *args]
    if child is not None:
        argv += ["--", sys.executable, "-c", child]
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=ROOT)
    assert out.exists(), f"recorder wrote nothing:\n{proc.stdout}\n{proc.stderr}"
    return out.read_text(), proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------

UNGUESSED_SECRET_NAMES = [
    "PAC_DB_PASSWORD",          # the obvious one, matched by the old rule too
    "PAC_EVALUATOR_LOGIN",      # a credentials file; "LOGIN" was not on the list
    "PGPASSFILE",               # libpq reads a password from here
    "BEDROCK_BEARER",           # a bearer token by another name
    "PAC_ADMIN_DSN",            # a whole connection string
    "SOMETHING_NOBODY_ADDED",   # the case the allowlist exists for
]


@pytest.mark.parametrize("name", UNGUESSED_SECRET_NAMES)
def test_an_env_value_never_reaches_the_record(tmp_path, sentinel, name):
    """Default deny. A name absent from the allowlist is withheld whether or
    not anyone anticipated it."""
    contents, _ = run_recorder(tmp_path, "--env", f"{name}={sentinel}")
    assert sentinel not in contents, f"{name} leaked its value into the record"


@pytest.mark.parametrize("name", UNGUESSED_SECRET_NAMES)
def test_the_variable_is_still_recorded_as_present(tmp_path, sentinel, name):
    """Withholding a value must not hide the fact that the run was
    configured -- that is the part a reviewer needs."""
    contents, _ = run_recorder(tmp_path, "--env", f"{name}={sentinel}")
    overrides = json.loads(contents)["command"]["env_overrides"]
    assert name in overrides or any(k.startswith("<") for k in overrides), (
        "the override vanished entirely; a reader cannot tell it was set")


def test_an_allowlisted_value_is_recorded(tmp_path):
    """The allowlist has to be worth having: values that describe WHAT ran
    are still written, or every record becomes unreadable."""
    contents, _ = run_recorder(tmp_path, "--env", "PAC_LLM_PROVIDER=offline")
    assert json.loads(contents)["command"]["env_overrides"]["PAC_LLM_PROVIDER"] \
        == "offline"


def test_no_allowlisted_name_looks_like_a_credential():
    """A guard on the allowlist itself. Adding PAC_DB_PASSWORD to it would
    silently undo every test above."""
    offenders = [n for n in rec.ENV_VALUE_ALLOWLIST if rec.SECRET_NAME.search(n)]
    assert not offenders, f"credential-shaped names on the allowlist: {offenders}"


# ---------------------------------------------------------------------------
# Connection strings, in output nobody controls
# ---------------------------------------------------------------------------

CONNECTION_STRINGS = [
    pytest.param(
        "host=db.internal port=5432 dbname=pharma user=pac_owner password={s}",
        id="libpq-keyword-form"),
    pytest.param(
        "host=db.internal user=pac_owner password='{s}' sslmode=require",
        id="libpq-quoted-value"),
    pytest.param(
        "postgresql://pac_owner:{s}@db.internal:5432/pharma",
        id="postgresql-url"),
    pytest.param(
        "postgres://pac_owner:{s}@db.internal/pharma",
        id="postgres-url-short-scheme"),
    pytest.param("aws_secret_access_key={s}", id="aws-secret-key"),
    pytest.param("Authorization: Bearer {s}", id="bearer-token"),
    pytest.param("sk-ant-{s}", id="anthropic-api-key"),
    pytest.param("Cookie: pac_session={s}", id="session-cookie"),
]


@pytest.mark.parametrize("template", CONNECTION_STRINGS)
def test_a_credential_printed_by_the_child_never_reaches_the_record(
        tmp_path, sentinel, template):
    """There is no variable name here to match on -- only the shape."""
    line = template.format(s=sentinel)
    contents, _ = run_recorder(
        tmp_path, child=f"print({line!r})")
    assert sentinel not in contents, f"leaked from stdout: {template}"


@pytest.mark.parametrize("template", CONNECTION_STRINGS)
def test_a_credential_printed_by_the_child_is_not_echoed_either(
        tmp_path, sentinel, template):
    """A CI log is as durable as a committed file and usually more widely
    read, so the terminal copy is scrubbed too."""
    line = template.format(s=sentinel)
    _, echoed = run_recorder(tmp_path, child=f"print({line!r})")
    assert sentinel not in echoed


def test_a_credential_in_an_exception_never_reaches_the_record(
        tmp_path, sentinel):
    """psycopg puts the connection string in the error when a connection
    fails. That text arrives through a traceback the recorder never chose."""
    dsn = f"host=db.internal user=pac_owner password={sentinel}"
    contents, _ = run_recorder(
        tmp_path,
        child=f"raise RuntimeError('connection to {dsn} failed')")
    assert sentinel not in contents


def test_the_failure_is_still_visible_after_scrubbing(tmp_path, sentinel):
    """Redaction must not destroy the diagnosis: a reader has to be able to
    tell that the run failed and roughly why."""
    dsn = f"host=db.internal user=pac_owner password={sentinel}"
    contents, _ = run_recorder(
        tmp_path,
        child=f"raise RuntimeError('connection to {dsn} failed')")
    record = json.loads(contents)
    assert record["outcome"]["status"] == "failed"
    assert record["outcome"]["exit_code"] != 0
    assert "RuntimeError" in (record["outcome"]["summary"] or "")


# ---------------------------------------------------------------------------
# Nested values
# ---------------------------------------------------------------------------

def test_a_secret_nested_at_depth_is_removed(sentinel):
    """The record is a tree. A scrubber that only walks the top level
    protects the fields somebody already thought about."""
    deep = {
        "outcome": {
            "attempts": [
                {"ordinal": 1, "error": None},
                {"ordinal": 2,
                 "error": f"could not connect: password={sentinel}",
                 "context": {"tried": [f"postgresql://u:{sentinel}@h/db"]}},
            ],
        },
    }
    assert sentinel not in json.dumps(rec._scrub(deep))


def test_a_secret_named_key_at_depth_is_removed(sentinel):
    nested = {"a": {"b": [{"db_password": sentinel, "note": "fine"}]}}
    scrubbed = rec._scrub(nested)
    assert scrubbed["a"]["b"][0]["db_password"] == rec.REDACTED
    assert scrubbed["a"]["b"][0]["note"] == "fine"


def test_scrubbing_preserves_the_shape_of_the_record(sentinel):
    """Structure must survive: a reviewer reads these by field."""
    original = {"list": [1, 2, {"tuple_becomes_list": ("a", "b")}],
                "none": None, "number": 7, "bool": True,
                "text": f"password={sentinel}"}
    scrubbed = rec._scrub(original)
    assert scrubbed["list"][0] == 1
    assert scrubbed["list"][2]["tuple_becomes_list"] == ["a", "b"]
    assert scrubbed["none"] is None
    assert scrubbed["number"] == 7
    assert scrubbed["bool"] is True
    assert sentinel not in scrubbed["text"]


def test_ordinary_text_is_left_alone():
    """Over-redaction is its own failure: a record of <redacted> establishes
    nothing."""
    ordinary = "419 passed in 12.31s"
    assert rec._scrub(ordinary) == ordinary


def test_the_whole_record_is_scrubbed_not_only_the_environment(
        tmp_path, sentinel):
    """The scrub runs over the assembled record, so a field added later is
    covered without anyone remembering to redact it."""
    contents, _ = run_recorder(
        tmp_path,
        "--claim", f"connecting with password={sentinel} should be recorded",
        child="print('ok')")
    assert sentinel not in contents
    assert "<redacted>" in json.loads(contents)["claim"]
