#!/usr/bin/env bash
# Post-deployment smoke test.
#
#   ./infra/smoke.sh https://your-url            # unauthenticated checks only
#   ./infra/smoke.sh https://your-url logins.json  # full role checks
#
# Exits non-zero on the first failure, so it is usable as a release gate.

set -uo pipefail

BASE="${1:?usage: smoke.sh <base-url> [evaluator_logins.json]}"
LOGINS="${2:-}"
JAR="$(mktemp -d)/cookies"
FAILURES=0

pass() { printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; FAILURES=$((FAILURES + 1)); }

echo "== availability =="
code=$(curl -fsS -o /dev/null -w '%{http_code}' "$BASE/health" 2>/dev/null || echo 000)
[ "$code" = "200" ] && pass "/health" || fail "/health returned $code"

ready=$(curl -fsS "$BASE/ready" 2>/dev/null || echo '{}')
if echo "$ready" | grep -q '"status": *"ready"'; then
  pass "/ready — dataset $(echo "$ready" | sed -n 's/.*"dataset": *"\([^"]*\)".*/\1/p')"
else
  fail "/ready not ready: $ready"
fi

code=$(curl -fsS -o /dev/null -w '%{http_code}' "$BASE/" 2>/dev/null || echo 000)
[ "$code" = "200" ] && pass "chat UI served" || fail "UI returned $code"

echo "== unauthenticated access is refused =="
for path in /api/me /api/conversations; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")
  [ "$code" = "401" ] && pass "$path -> 401" || fail "$path -> $code (expected 401)"
done

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{"question":"top accounts by pack units"}' "$BASE/api/ask")
[ "$code" = "401" ] && pass "/api/ask -> 401" || fail "/api/ask -> $code (expected 401)"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -H 'Cookie: pac_session=forged' -H 'X-Role: exec' \
  -d '{"question":"revenue"}' "$BASE/api/ask")
[ "$code" = "401" ] && pass "forged cookie + role header -> 401" \
  || fail "forged credentials -> $code (expected 401)"

if [ -z "$LOGINS" ]; then
  echo; echo "skipped role checks (pass evaluator_logins.json as the 2nd argument)"
  [ "$FAILURES" -eq 0 ] && { echo "ALL PASS"; exit 0; } || { echo "$FAILURES FAILED"; exit 1; }
fi

read_login() { python3 -c "
import json,sys
for x in json.load(open('$LOGINS')):
    if x['role'] == '$1':
        print(x['$2']); break
"; }

echo "== roles see different scopes =="
for role in exec director ram; do
  email=$(read_login "$role" email); password=$(read_login "$role" password)
  [ -z "$email" ] && { fail "no $role in $LOGINS"; continue; }

  body=$(curl -s -c "$JAR.$role" -X POST -H 'Content-Type: application/json' \
    -d "$(python3 -c "import json,sys;print(json.dumps({'email':'$email','password':'$password'}))")" \
    "$BASE/api/login")
  scope=$(echo "$body" | sed -n 's/.*"scope": *"\([^"]*\)".*/\1/p')
  [ -n "$scope" ] && pass "$role signs in — scope: $scope" || { fail "$role login failed: $body"; continue; }

  ans=$(curl -s -b "$JAR.$role" -X POST -H 'Content-Type: application/json' \
    -d '{"question":"What are the top 3 accounts by pack units this quarter?"}' "$BASE/api/ask")
  echo "$ans" | grep -q '"status": *"answered"' \
    && pass "$role gets an answer" || fail "$role answer failed: $(echo "$ans" | head -c 160)"

  rev=$(curl -s -b "$JAR.$role" -X POST -H 'Content-Type: application/json' \
    -d '{"question":"What is our total revenue in dollars this quarter?","include_sql":true}' \
    "$BASE/api/ask")
  if [ "$role" = "exec" ]; then
    echo "$rev" | grep -q '\$' && pass "exec sees pricing" || fail "exec did not get pricing"
  else
    if echo "$rev" | grep -qi '"sql":[^,]*wac'; then
      fail "$role SQL CONTAINS WAC — LEAK"
    else
      pass "$role gets no WAC in SQL"
    fi
    echo "$rev" | grep -qi 'packs\|volume\|restricted' \
      && pass "$role offered a volume alternative" \
      || fail "$role got neither volume nor an explanation"
  fi
done

echo "== data-quality warning is surfaced =="
share=$(curl -s -b "$JAR.exec" -X POST -H 'Content-Type: application/json' \
  -d '{"question":"What is our market share for Zenovax?"}' "$BASE/api/ask")
echo "$share" | grep -qi 'market source contains only competitor\|not a real\|inconsistent' \
  && pass "impossible share is labelled" || fail "no data-quality warning on market share"

echo
if [ "$FAILURES" -eq 0 ]; then echo "ALL PASS"; exit 0; else echo "$FAILURES FAILED"; exit 1; fi
