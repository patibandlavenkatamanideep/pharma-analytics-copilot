#!/bin/bash
# Run the candidate's regression tests against application code that lacks the fix.
#
#   evidence/probes/replay_on_baseline.sh <baseline-commit> <name> <test-file>...
#
# A detached worktree at <baseline-commit> receives the candidate's tests/ directory
# (so helpers and fixtures match) and the candidate's evidence recorder. The
# application code and every other script stay as they were at the baseline: a fix
# that lives in scripts/ is NOT copied, so its absence is part of what is measured.
# The record names each added or changed file by digest, and the JUnit file shows
# which tests failed and with what exception type, sanitised by
# evidence/probes/sanitize_junit.py (no traceback or captured output). Writes
# evidence/runs/<name>.json and evidence/runs/<name>.junit.xml in this checkout.
set -euo pipefail
BASE=$(git rev-parse --verify "${1:?baseline}^{commit}"); NAME=${2:?name}; shift 2
[ $# -gt 0 ] || { echo "no test files" >&2; exit 2; }
REPO=$(git rev-parse --show-toplevel)
PY=${PY:-$HOME/.venvs/pac-release/bin/python}
W=$(mktemp -d "${TMPDIR:-/tmp}/pac-replay.XXXXXX")
cleanup() { git -C "$REPO" worktree remove --force "$W" >/dev/null 2>&1 || rm -rf "$W"; git -C "$REPO" worktree prune; }
trap cleanup EXIT
git -C "$REPO" worktree add --detach "$W" "$BASE" >/dev/null
rm -rf "$W/tests" && cp -R "$REPO/tests" "$W/tests"
# KEEP_BASELINE_RECORDER=1 when the recorder itself is what is being replayed.
[ "${KEEP_BASELINE_RECORDER:-0}" = 1 ] || cp "$REPO/scripts/record_evidence.py" "$W/scripts/record_evidence.py"
[ -f "$REPO/.env" ] && cp "$REPO/.env" "$W/.env"
cd "$W"
"$PY" scripts/record_evidence.py --out "$REPO/evidence/runs/$NAME.json" --provider-mode offline \
  --database "${PAC_DB_NAME:-pac_release}" \
  --claim "The candidate's regression tests for $NAME, run against application code at ${BASE:0:7} that lacks the fix (expected outcome: failed)" \
  --limit "tests/ and scripts/record_evidence.py are the candidate's, recorded by digest; application code and every other script are the baseline's" \
  -- "$PY" -m pytest "$@" -q -p no:cacheprovider --continue-on-collection-errors \
     --junitxml="$REPO/evidence/runs/$NAME.junit.xml" || true
# pytest writes failure messages verbatim; keep only exception type and first line.
"$PY" "$REPO/evidence/probes/sanitize_junit.py" "$REPO/evidence/runs/$NAME.junit.xml"
