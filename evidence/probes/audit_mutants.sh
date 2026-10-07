#!/bin/bash
# Mutants for step 5 of the qualification (strict audit): each must make at
# least one test in tests/security/test_audit_modes.py fail. Each mutated
# file is restored with git checkout, so this refuses to run on a tree with
# uncommitted changes, which that would discard.
set -u
cd "$(dirname "$0")/../.."
PY=${PY:-python3}
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "refusing: uncommitted changes would be reverted by git checkout"; exit 2
fi
T=(tests/security/test_audit_modes.py)
caught=0; total=0
mut() {
  local name=$1 file=$2 old=$3 new=$4
  python3 - "$file" "$old" "$new" <<'EOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
assert s.count(sys.argv[2]) == 1, ("not unique", sys.argv[2])
p.write_text(s.replace(sys.argv[2], sys.argv[3]))
EOF
  local out; out=$($PY -m pytest -q -p no:cacheprovider "${T[@]}" 2>&1 | tail -1)
  git checkout -q -- "$file"; total=$((total+1)); case "$out" in *failed*) caught=$((caught+1));; esac
  echo "$name: $out"
}
mut "strict audit written after the commit, not in it" app/pipeline.py \
  '                    done = finalise(self.principal, self.state, self.run, turn,
                                    result.payload, audit=in_commit)' \
  '                    done = finalise(self.principal, self.state, self.run, turn,
                                    result.payload, audit=None)'
mut "strict releases the answer when the commit fails" app/pipeline.py \
  '                runs.fail(self.run, None)
                raise AuditUnavailable()' \
  '                runs.fail(self.run, None)'
mut "strict does not record a replay" app/pipeline.py \
  '                self._record_replay(principal, run, replayed, dataset)' \
  '                pass'
mut "a failed replay record still releases the replay" app/pipeline.py \
  '            telemetry.count("pac.persistence.failures", kind="audit")
            raise AuditUnavailable() from None' \
  '            telemetry.count("pac.persistence.failures", kind="audit")'
mut "audit hook ignored by finalise" app/conversation/state.py \
  '            if audit is not None:
                audit(cur)' \
  '            if False:
                audit(cur)'
dirty=$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')
echo "mutants: $caught of $total caught; files left modified: $dirty"
[ "$caught" -eq "$total" ] && [ "$dirty" -eq 0 ]
