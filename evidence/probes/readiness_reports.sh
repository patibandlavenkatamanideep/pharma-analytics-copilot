#!/bin/bash
# Readiness reports for the supplied dataset and both fixture profiles.
#   evidence/probes/readiness_reports.sh <out-dir>
# Rebuilds each profile into its disposable database (names must contain
# "profile"; prefix from PAC_PROFILE_DB_PREFIX), reconciles its onboarding,
# and writes the readiness report of each, plus the report of the database
# named by PAC_DB_NAME (the supplied data). Read-only on PAC_DB_NAME.
set -euo pipefail
out=${1:?out-dir}
mkdir -p "$out"
root=$(cd "$(dirname "$0")/../.." && pwd)
py=${PY:-python3}
prefix=${PAC_PROFILE_DB_PREFIX:-pac_profile_}
for profile in orchard estuary; do
  work=$(mktemp -d)
  PYTHONPATH="$root" "$py" "$root/scripts/build_profile_db.py" --profile "$profile" \
      --db "${prefix}${profile}" --dir "$work/data" --report "$out/$profile-onboarding.json" >/dev/null
  PYTHONPATH="$root" "$py" "$root/scripts/readiness_report.py" --db "${prefix}${profile}" \
      --onboarding "$out/$profile-onboarding.json" --out "$out/$profile-readiness.json"
  rm -rf "$work"
done
PYTHONPATH="$root" "$py" "$root/scripts/readiness_report.py" --db "${PAC_DB_NAME:?}" --real-data no \
    --out "$out/supplied-readiness.json"
