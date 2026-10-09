#!/usr/bin/env bash
# Do the unit tests pass in a checkout like the one CI's test job gets?
#
#   PYTHON=<python> bash evidence/probes/shallow_checkout_tests.sh [depth]
#
# Clones this repository's current branch with --depth <depth> (1 is what
# actions/checkout fetches by default; 0 means the whole history) into a
# temporary directory and runs the unit suite there, with no database
# reachable. A test that reads Git history -- the ledger's fix commits --
# passes on a full clone and fails on a shallow one. Exit: pytest's.
set -euo pipefail
depth=${1:-1}
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/pac-shallow.XXXXXX")
trap 'rm -rf "$work"' EXIT
args=(--quiet --branch "$(git -C "$root" rev-parse --abbrev-ref HEAD)")
[ "$depth" != 0 ] && args+=(--depth "$depth")
git clone "${args[@]}" "file://$root" "$work/repo"
cd "$work/repo"
echo "clone at $(git rev-parse --short HEAD) with $(git rev-list --count HEAD) commit(s)"
PAC_DB_HOST=127.0.0.1 PAC_DB_PORT=1 PAC_DB_NAME=pac_unreachable PAC_LLM_PROVIDER=offline \
  AWS_EC2_METADATA_DISABLED=true \
  "${PYTHON:-python3}" -m pytest tests/unit -q -p no:cacheprovider
