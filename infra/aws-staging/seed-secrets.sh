#!/usr/bin/env bash
# Give the staging secret containers their first values. Run by the owner,
# after the first apply, with an expiring session (IAM Identity Center):
#
#   AWS_PROFILE=<sso profile> infra/aws-staging/seed-secrets.sh \
#       [--reviewers reviewers.csv] [user_id ...]
#
# * db/owner, db/auth, db/exec, db/scoped: a random password each. The
#   bootstrap task sets them on the database roles.
# * test-users: when user ids are given (disposable staging identities from
#   the loaded dataset), a JSON object of user id -> random password. The
#   test-users task sets them; the owner hands each tester their own.
# * reviewers: with --reviewers, the accounts the owner approved, from a CSV
#   of email,name,like (and optionally disabled). Run it again with the
#   edited CSV to add or remove reviewers: a reviewer already listed keeps
#   their password, a new one gets a random one, and one dropped from the CSV
#   is disabled (scripts/provision_reviewers.py --merge-csv). Then run the
#   reviewers task. Keep the CSV out of the repository (*.csv is ignored here).
#
# Other containers that already hold a value are left alone: changing a live
# password here would lock the serving tasks out until bootstrap runs again.
# Values are generated locally and piped to Secrets Manager -- never an
# argument (visible to other processes), never printed, never in a file.
# Output is secret names, "set" / "already set" and the reviewer counts only.
#
# Prepared, never run: no staging account existed when it was written.

set -euo pipefail

reviewers_csv=""
if [ "${1:-}" = "--reviewers" ]; then
  reviewers_csv=${2:?--reviewers needs the CSV file}
  [ -r "$reviewers_csv" ] || { echo "cannot read $reviewers_csv" >&2; exit 1; }
  reviewers_csv=$(cd "$(dirname "$reviewers_csv")" && pwd)/$(basename "$reviewers_csv")
  shift 2
fi
cd "$(dirname "$0")"

command -v aws >/dev/null || { echo "aws CLI v2 is required" >&2; exit 1; }
arns=$(terraform output -json secret_arns)

arn_of() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$arns" "$1"; }

has_value() {
  [ "$(aws secretsmanager list-secret-version-ids --secret-id "$1" \
        --query 'length(Versions[?contains(VersionStages, `AWSCURRENT`)])' --output text)" != "0" ]
}

put() {  # put <name> <arn>, value on stdin
  aws secretsmanager put-secret-value --secret-id "$2" --secret-string file:///dev/stdin \
    --query Name --output text >/dev/null
  echo "$1: set"
}

for role in owner auth exec scoped; do
  arn=$(arn_of "db_$role")
  if has_value "$arn"; then echo "db/$role: already set"; continue; fi
  python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))' | put "db/$role" "$arn"
done

if [ "$#" -gt 0 ]; then
  arn=$(arn_of test_users)
  if has_value "$arn"; then
    echo "test-users: already set (delete its value to replace the list)"
  else
    python3 -c 'import json, secrets, sys
json.dump({u: secrets.token_urlsafe(18) for u in sys.argv[1:]}, sys.stdout)' "$@" | put test-users "$arn"
  fi
fi

if [ -n "$reviewers_csv" ]; then
  arn=$(arn_of reviewers)
  # Held in this shell, never an argument or a file; an invalid CSV writes
  # nothing (the merge exits 2 and the pipeline fails before put).
  if has_value "$arn"; then
    merged=$(aws secretsmanager get-secret-value --secret-id "$arn" --query SecretString --output text \
      | python3 ../../scripts/provision_reviewers.py --merge-csv "$reviewers_csv")
  else
    merged=$(echo '[]' | python3 ../../scripts/provision_reviewers.py --merge-csv "$reviewers_csv")
  fi
  printf '%s' "$merged" | put reviewers "$arn"
  unset merged
fi
