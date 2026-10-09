#!/bin/bash
# Mutants for step 4 of the qualification (classification authority and the
# calendar check at load): each must make at least one test fail. Run on a
# clean tree with the disposable databases configured (PAC_DB_NAME,
# PAC_PROFILE_DB_PREFIX); each mutated file is restored with git checkout.
set -u
cd "$(dirname "$0")/../.."
PY=${PY:-python3}
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "refusing: uncommitted changes would be reverted by git checkout"; exit 2
fi
T=(tests/unit/test_semantics.py tests/unit/test_classification_mapping.py tests/unit/test_segment_share.py
   tests/integration/test_classification_authority.py tests/integration/test_fixture_profiles.py)
caught=0
total=0
mut() {
  local name=$1 file=$2 old=$3 new=$4
  python3 - "$file" "$old" "$new" <<'EOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
assert s.count(sys.argv[2]) == 1, ("not unique", sys.argv[2])
p.write_text(s.replace(sys.argv[2], sys.argv[3]))
EOF
  local out
  out=$($PY -m pytest -q -p no:cacheprovider "${T[@]}" 2>&1 | tail -1)
  git checkout -q -- "$file"
  total=$((total + 1))
  case "$out" in *failed*) caught=$((caught + 1));; esac
  echo "$name: $out"
}
mut "elimination restored" app/data/classification.py \
  '    return Classified("unknown", "brand_flag=0 and no mapping entry", "none")' \
  '    return Classified("branded_competitor", "elimination", "none")'
mut "unknown volume ignored in upper bound" app/analytics/compiler.py \
  '"            THEN (COALESCE(n.value, 0) + COALESCE(u.value, 0))"' \
  '"            THEN (COALESCE(n.value, 0) + 0*COALESCE(u.value, 0))"'
mut "all-unknown market gets a share" app/analytics/compiler.py \
  '"CASE WHEN COALESCE(u.value, 0) < d.value\n"
                "            THEN COALESCE(n.value, 0) / NULLIF' \
  '"CASE WHEN COALESCE(u.value, 0) <= d.value\n"
                "            THEN COALESCE(n.value, 0) / NULLIF'
mut "contradiction accepted" app/data/classification.py \
  '        if mapped is not None and mapped != "company_brand":' '        if False:'
mut "subcategory ignored in key" app/data/classification.py \
  '    mapped = mapping.entries.get((name, subcategory)) if mapping is not None else None' \
  '    mapped = next((c for (n, _), c in mapping.entries.items() if n == name), None) if mapping is not None else None'
mut "profile mapping ignored" app/data/loader.py \
  '        return read_mapping(beside) if beside.exists() else None' '        return None'
mut "unknown volume not carried" app/analytics/compiler.py \
  '        bounded = (spec.get("unknown_class_bound") and bool(plan.filters.classifications)' \
  '        bounded = (False and bool(plan.filters.classifications)'
T=(tests/integration/test_calendar_convention_at_load.py)
mut "calendar check reads one week" app/data/loader.py \
  '        convention = detect_convention(weeks)
    except ConventionError as exc:
        report.source_coverage["calendar"]' \
  '        convention = detect_convention(weeks[:1])
    except ConventionError as exc:
        report.source_coverage["calendar"]'
dirty=$(git status --porcelain | wc -l | tr -d ' ')
echo "mutants: $caught of $total caught; files left modified: $dirty"
[ "$caught" -eq "$total" ] && [ "$dirty" -eq 0 ]
