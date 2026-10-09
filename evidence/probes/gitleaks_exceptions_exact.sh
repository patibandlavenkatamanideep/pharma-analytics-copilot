#!/bin/bash
# Are the .gitleaksignore exceptions exact, or could they hide a real secret?
#   evidence/probes/gitleaks_exceptions_exact.sh
# Each exception is a commit:file:rule:line fingerprint for the public CPython
# release key that trivy reports quote. In a throwaway clone of HEAD this
# commits two planted findings and scans that commit only:
#   1. the same public GPG_KEY value, in a new file;
#   2. a random value of the excepted rule at an excepted path and line
#      (evidence/runs/r5-candidate-trivy.json, generic-api-key, line 50), and an
#      AWS-access-key-shaped one below it -- in a different commit.
# Exact exceptions suppress none of them. Exit 0 when gitleaks reports all three.
# Neither value appears literally here: the public one is read from the
# commit that introduced it, the other is generated per run.
set -euo pipefail
root=${PAC_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}
gitleaks=${GITLEAKS:-gitleaks}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git clone -q "$root" "$tmp/repo"
cd "$tmp/repo"
public=$(git show ef061f1:evidence/runs/r5-candidate-trivy.json | sed -n 50p)
mkdir -p evidence/runs
printf '%s\n' "$public" > evidence/runs/probe-same-public-value.json
python3 - <<'PY'
import secrets, string
alphabet = string.ascii_uppercase + "234567"
key = "AKIA" + "".join(secrets.choice(alphabet) for _ in range(16))
hexkey = "".join(secrets.choice("0123456789ABCDEF") for _ in range(40))
lines = [f'    "line {i}"' for i in range(1, 50)]
lines.append(f'      "GPG_KEY={hexkey}",')          # line 50: the excepted rule and line
lines.append(f'      "AWS_ACCESS_KEY_ID={key}",')   # line 51
open("evidence/runs/r5-candidate-trivy.json", "w").write("\n".join(lines) + "\n")
PY
git add -A evidence/runs
git -c user.name=probe -c user.email=probe@example.invalid commit -q -m "probe: planted findings"
report=$tmp/report.json
"$gitleaks" git . --redact --no-banner --no-color --log-opts="-1" \
  --report-format json --report-path "$report" > /dev/null 2>&1 || true
python3 - "$report" <<'PY'
import json, sys
found = json.load(open(sys.argv[1]))
got = {(f["File"], f["RuleID"], f["StartLine"]) for f in found}
want = {("evidence/runs/probe-same-public-value.json", "generic-api-key", 1),
        ("evidence/runs/r5-candidate-trivy.json", "generic-api-key", 50),
        ("evidence/runs/r5-candidate-trivy.json", "aws-access-token", 51)}
print(json.dumps({"findings": sorted(map(list, got)), "planted": len(want),
                  "all_planted_findings_reported": want <= got}))
sys.exit(0 if want <= got else 1)
PY
