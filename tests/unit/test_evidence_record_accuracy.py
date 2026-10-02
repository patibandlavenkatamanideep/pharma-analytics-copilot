"""An evidence record has to be true about the run it describes.

The redaction tests next door check that a record says too little. These
check the opposite failure: that what it does say is right. A record is the
only thing standing behind a claim in docs/PRODUCTION_UPGRADE.md, so a field
that is quietly wrong is worse than a field that is absent -- absence is
visible.

The defect that prompted these: ``_git()`` strips its output before the
caller splits it into lines. ``git status --porcelain`` puts a two-character
status field at the start of every line, and for an unstaged change the first
of those characters is a space. Stripping removed it, so the first path in
every record lost its first character -- ``app/config.py`` was recorded as
``pp/config.py``. Every record written before this commit carries it.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
RECORDER = ROOT / "scripts" / "record_evidence.py"

spec = importlib.util.spec_from_file_location("record_evidence", RECORDER)
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """A real git repository, because the thing under test is git's output
    format and a stub would encode my belief about it rather than the fact."""
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text("original\n")
    (tmp_path / "keep.txt").write_text("unchanged\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    monkeypatch.setattr(rec, "ROOT", tmp_path)
    return tmp_path


def test_an_unstaged_change_keeps_its_first_character(repo):
    """The regression. An unstaged modification is reported as ' M path',
    and the leading space is part of the status, not of the path."""
    (repo / "app" / "config.py").write_text("changed\n")

    paths = rec._dirty_paths()

    assert paths == ["app/config.py"], (
        "the first dirty path lost characters to the status field")


def test_a_staged_change_is_reported_at_the_same_path(repo):
    (repo / "app" / "config.py").write_text("changed\n")
    subprocess.run(["git", "add", "app/config.py"], cwd=repo, check=True,
                   capture_output=True)

    assert rec._dirty_paths() == ["app/config.py"]


def test_an_untracked_file_is_reported(repo):
    (repo / "new_test.py").write_text("x\n")

    assert rec._dirty_paths() == ["new_test.py"]


def test_several_changes_are_all_reported_in_full(repo):
    """The bug only ever damaged the FIRST path, so a single-file check
    would have passed against the broken version."""
    (repo / "app" / "config.py").write_text("changed\n")
    (repo / "keep.txt").write_text("also changed\n")
    (repo / "added.py").write_text("x\n")

    assert rec._dirty_paths() == ["added.py", "app/config.py", "keep.txt"]


def test_a_path_containing_a_space_survives(repo):
    """git quotes such paths in the line-oriented form. -z does not."""
    (repo / "a file with spaces.md").write_text("x\n")

    assert rec._dirty_paths() == ["a file with spaces.md"]


def test_a_rename_is_reported_once_not_as_two_entries(repo):
    """A rename entry is followed by its origin path as a separate field.
    Read as a status line it would produce a garbage path."""
    subprocess.run(["git", "mv", "keep.txt", "moved.txt"], cwd=repo,
                   check=True, capture_output=True)

    paths = rec._dirty_paths()

    assert paths == ["moved.txt"], f"rename produced {paths}"


def test_a_clean_worktree_reports_clean(repo):
    assert rec._dirty_paths() == []
    assert rec._application()["worktree_clean"] is True


def test_a_dirty_worktree_is_never_reported_as_clean(repo):
    """The field a reviewer relies on to know whether the recorded SHA
    actually describes what ran."""
    (repo / "app" / "config.py").write_text("changed\n")

    assert rec._application()["worktree_clean"] is False


# ---------------------------------------------------------------------------
# The record as a whole
# ---------------------------------------------------------------------------

def test_the_recorded_exit_code_is_the_child_s(tmp_path):
    out = tmp_path / "r.json"
    subprocess.run(
        [sys.executable, str(RECORDER), "--out", str(out), "--",
         sys.executable, "-c", "import sys; sys.exit(3)"],
        capture_output=True, text=True, cwd=ROOT)

    record = json.loads(out.read_text())
    assert record["outcome"]["exit_code"] == 3
    assert record["outcome"]["status"] == "failed"


def test_a_run_with_no_command_is_not_recorded_as_passed(tmp_path):
    """Recording a claim without running anything must not produce a green
    record; 'not_run' is the honest status."""
    out = tmp_path / "r.json"
    subprocess.run(
        [sys.executable, str(RECORDER), "--out", str(out),
         "--claim", "nothing was executed"],
        capture_output=True, text=True, cwd=ROOT)

    assert json.loads(out.read_text())["outcome"]["status"] == "not_run"


# ---------------------------------------------------------------------------
# Which bytes ran (review of 1 October 2026: a SHA alone does not identify
# an uncommitted tree)
# ---------------------------------------------------------------------------

def test_a_record_names_the_tree_it_ran(repo):
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    app = rec._application()
    assert app["tree"] == tree and app["dirty_digests"] is None


def test_a_dirty_tree_records_each_changed_file_by_digest(repo):
    import hashlib
    (repo / "app" / "config.py").write_text("changed\n")
    (repo / "new.txt").write_text("untracked\n")
    (repo / "keep.txt").unlink()
    digests = rec._application()["dirty_digests"]
    assert digests == {"app/config.py": hashlib.sha256(b"changed\n").hexdigest(),
                       "new.txt": hashlib.sha256(b"untracked\n").hexdigest(),
                       "keep.txt": None}


def test_digests_survive_the_credential_scrub(repo):
    (repo / "app" / "config.py").write_text("changed\n")
    record = rec._scrub({"application": rec._application()})
    assert all(len(d) == 64 for d in record["application"]["dirty_digests"].values() if d)


def test_require_clean_refuses_to_run_a_release_check_on_a_dirty_tree(repo, tmp_path,
                                                                       monkeypatch):
    (repo / "app" / "config.py").write_text("changed\n")
    out, marker = tmp_path / "r.json", tmp_path / "ran"
    monkeypatch.setattr(sys, "argv", ["record_evidence.py", "--require-clean", "--out", str(out),
                                      "--", sys.executable, "-c",
                                      f"open({str(marker)!r}, 'w').close()"])
    assert rec.main() == 2
    record = json.loads(out.read_text())
    assert record["outcome"]["status"] == "blocked" and record["outcome"]["exit_code"] is None
    assert "uncommitted" in record["outcome"]["blocked_reason"]
    assert not marker.exists(), "the command ran on a dirty tree"


def test_an_image_is_recorded_by_what_the_engine_reports(tmp_path):
    engine = tmp_path / "engine"
    engine.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps([{"
                      "'Id': 'sha256:' + 'a' * 64, 'Digest': 'sha256:' + 'b' * 64, "
                      "'Os': 'linux', 'Architecture': 'amd64', "
                      "'Config': {'Labels': {'org.opencontainers.image.revision': 'c' * 40}}}]))\n")
    engine.chmod(0o755)
    image = rec._image("pharma-analytics-copilot:ci", str(engine))
    assert image == {"ref": "pharma-analytics-copilot:ci", "inspected_with": str(engine),
                     "id": "sha256:" + "a" * 64, "digest": "sha256:" + "b" * 64,
                     "platform": "linux/amd64", "revision_label": "c" * 40}
    assert rec._image("missing", str(tmp_path / "no-such-engine"))["id"] is None
