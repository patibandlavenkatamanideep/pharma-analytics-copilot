#!/usr/bin/env bash
# Build the image and test the artifact itself -- what CI's image job does,
# and further: readiness with data loaded, an authenticated journey per role,
# a clean stop, and a vulnerability scan of the exact image.
#
#   scripts/image_smoke.sh                      # with docker
#   CONTAINER_CLI=podman scripts/image_smoke.sh # with podman
#
# It builds the COMMITTED tree at HEAD; commit first.
#
# Everything runs in throwaway containers on a private network: a fresh
# postgres:16-alpine, the image provisioning its own database, a jobs-style
# container loading seed data, and the app container. Passwords are random
# per run and never written to disk. Nothing outside the containers is
# touched; they are removed on exit.
set -euo pipefail

CLI=${CONTAINER_CLI:-docker}
TAG=pharma-analytics-copilot:smoke
TRIVY_IMAGE=${TRIVY_IMAGE:-docker.io/aquasec/trivy:0.58.1}
RELEASE=$(git rev-parse HEAD)
NET=pac-smoke-net DB=pac-smoke-db APP=pac-smoke-app
PORT=${PAC_SMOKE_PORT:-18000}
BASE="http://127.0.0.1:$PORT"
WORK=$(mktemp -d)
pw() { openssl rand -hex 16; }
SUPER=$(pw) OWNER=$(pw) AUTH=$(pw) EXEC=$(pw) SCOPED=$(pw) USERPW=$(pw)
declare -a RESULTS=()

cleanup() {
  $CLI rm -f "$APP" "$DB" >/dev/null 2>&1 || true
  $CLI network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
pass() { echo "PASS  $*"; RESULTS+=("PASS: $*"); }
fail() { echo "FAIL  $*"; RESULTS+=("FAIL: $*"); summary; exit 1; }
summary() {
  printf '%s\n' "${RESULTS[@]}" > "$WORK/results.txt"
  python3 - "$WORK/results.txt" "$RELEASE" <<'PY'
import json, sys
lines = open(sys.argv[1]).read().splitlines()
print(json.dumps({"release": sys.argv[2], "checks": lines,
                  "passed": sum(l.startswith("PASS") for l in lines),
                  "failed": sum(l.startswith("FAIL") for l in lines)}))
PY
}
app_env=(-e PAC_DB_HOST="$DB" -e PAC_ENVIRONMENT=cloud -e PAC_LLM_PROVIDER=offline
         -e PAC_DB_AUTH_PASSWORD="$AUTH" -e PAC_DB_EXEC_PASSWORD="$EXEC"
         -e PAC_DB_SCOPED_PASSWORD="$SCOPED")
jobs_env=("${app_env[@]}" -e PAC_DB_OWNER_PASSWORD="$OWNER")

# -- build ------------------------------------------------------------------
# From the COMMIT, not the working directory: `git archive` of $RELEASE is the
# build context, so the image is provably that commit's tree, and untracked
# files (.env, local dumps) cannot enter it whatever .dockerignore says. It
# also avoids reading the checkout through the VM's file sharing, which
# failed on this machine for a file in a synced folder.
started=$(date +%s)
mkdir -p "$WORK/context"
git archive "$RELEASE" | tar -x -C "$WORK/context"
$CLI build --build-arg PAC_RELEASE="$RELEASE" -t "$TAG" "$WORK/context" > "$WORK/build.log" 2>&1 \
  || { tail -30 "$WORK/build.log"; fail "image builds"; }
pass "image builds ($(( $(date +%s) - started )) s)"
label=$($CLI image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$TAG")
[ "$label" = "$RELEASE" ] && pass "image is labelled with commit $RELEASE" || fail "revision label is '$label'"
size=$($CLI image inspect --format '{{.Size}}' "$TAG")
pass "image size $(( size / 1000000 )) MB"

# -- scan -------------------------------------------------------------------
# Through a volume rather than a bind mount: a container VM (podman machine,
# colima) need not share host directories, and a bind of a missing path is
# silently an empty directory.
VOL=pac-smoke-scan
$CLI volume rm -f "$VOL" >/dev/null 2>&1 || true
$CLI volume create "$VOL" >/dev/null
$CLI save -o "$WORK/image.tar" "$TAG" >/dev/null
$CLI run --rm -i -v "$VOL:/scan" docker.io/library/alpine:3.20 sh -c 'cat > /scan/image.tar' \
  < "$WORK/image.tar"
$CLI run --rm -v "$VOL:/scan" "$TRIVY_IMAGE" image --quiet --input /scan/image.tar \
  --severity HIGH,CRITICAL --ignore-unfixed --format json --output /scan/trivy.json \
  > "$WORK/trivy.log" 2>&1 || { tail -20 "$WORK/trivy.log"; fail "trivy ran"; }
$CLI run --rm -v "$VOL:/scan" docker.io/library/alpine:3.20 cat /scan/trivy.json > "$WORK/trivy.json"
$CLI volume rm -f "$VOL" >/dev/null 2>&1 || true
findings=$(python3 - "$WORK/trivy.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
vulns = [(t["Target"], v["PkgName"], v["InstalledVersion"], v["VulnerabilityID"], v["Severity"],
          v.get("FixedVersion"))
         for t in r.get("Results", []) for v in (t.get("Vulnerabilities") or [])]
for v in vulns:
    print("  ", *v, file=sys.stderr)
print(len(vulns))
PY
)
[ "$findings" = "0" ] && pass "trivy: no HIGH/CRITICAL vulnerability with a fix available" \
  || fail "trivy: $findings HIGH/CRITICAL vulnerabilities with a fix available"
cp "$WORK/trivy.json" "${TRIVY_REPORT:-/dev/null}" 2>/dev/null || true

# -- database ---------------------------------------------------------------
$CLI network create "$NET" >/dev/null
$CLI run -d --name "$DB" --network "$NET" -e POSTGRES_PASSWORD="$SUPER" \
  docker.io/library/postgres:16-alpine >/dev/null
for _ in $(seq 1 60); do $CLI exec "$DB" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

# -- refusals ---------------------------------------------------------------
$CLI run --rm --network "$NET" -e PAC_DB_HOST=127.0.0.1 -e PAC_LLM_PROVIDER=offline "$TAG" \
  timeout 25 uvicorn app.api.main:app --host 0.0.0.0 --port 8000 > "$WORK/nodb.log" 2>&1 || true
grep -qiE "connection refused|boundary" "$WORK/nodb.log" \
  && pass "refuses to serve with no database" || fail "started without a database"

$CLI run --rm --network "$NET" -e PAC_DB_HOST="$DB" \
  -e PAC_ADMIN_DSN="postgresql://postgres:$SUPER@$DB:5432/postgres" \
  -e PAC_DB_OWNER_PASSWORD="$OWNER" -e PAC_DB_AUTH_PASSWORD="$AUTH" \
  -e PAC_DB_EXEC_PASSWORD="$EXEC" -e PAC_DB_SCOPED_PASSWORD="$SCOPED" \
  "$TAG" python scripts/bootstrap_db.py --drop --no-env > "$WORK/bootstrap.log" 2>&1 \
  && pass "provisions its own database (roles, schema, policies)" \
  || { tail -20 "$WORK/bootstrap.log"; fail "bootstrap from inside the image"; }

$CLI run --rm --network "$NET" "${jobs_env[@]}" "$TAG" \
  timeout 25 uvicorn app.api.main:app --host 0.0.0.0 --port 8001 > "$WORK/owner.log" 2>&1 || true
grep -q "OWNER credential" "$WORK/owner.log" \
  && pass "refuses to serve while holding the owner credential" || fail "served with the owner credential"

# -- serving ----------------------------------------------------------------
$CLI run -d --name "$APP" --network "$NET" -p "127.0.0.1:$PORT:8000" "${app_env[@]}" \
  -e PAC_COOKIE_SECURE=false "$TAG" >/dev/null
for _ in $(seq 1 60); do curl -sf "$BASE/health" >/dev/null && break; sleep 1; done
release=$(curl -s "$BASE/health" | python3 -c "import json,sys; print(json.load(sys.stdin)['release'])")
[ "$release" = "$RELEASE" ] && pass "/health reports release $release" || fail "/health release '$release'"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")" = "503" ] \
  && pass "not ready before any data is published" || fail "ready with no data"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")" = "200" ] && pass "UI served from the image" || fail "UI"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/me")" = "401" ] && pass "unauthenticated /api/me is 401" || fail "/api/me"
user=$($CLI exec "$APP" id -un)
[ "$user" != "root" ] && pass "runs as '$user', not root" || fail "runs as root"

# -- data, through the jobs path --------------------------------------------
$CLI run --rm --network "$NET" "${jobs_env[@]}" "$TAG" \
  python scripts/load_data.py --mode seed > "$WORK/load.log" 2>&1 \
  && pass "seed data loaded by a jobs container" || { tail -20 "$WORK/load.log"; fail "load"; }
for _ in $(seq 1 30); do [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")" = "200" ] && break; sleep 1; done
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")" = "200" ] && pass "ready once a dataset is published" || fail "not ready after load"

emails=$($CLI run --rm --network "$NET" "${jobs_env[@]}" -e SMOKE_PW="$USERPW" "$TAG" python -c "
import os
from app.auth.identity import set_credential
from app.db import owner_transaction
with owner_transaction() as cur:
    cur.execute(\"SELECT DISTINCT ON (role) user_id, email, role FROM users WHERE role = 'exec' \"
                \"OR (role = 'ram' AND territory_name IN (SELECT territory_name FROM zip_territory)) \"
                \"ORDER BY role, user_id\")
    rows = cur.fetchall()
for r in rows:
    set_credential(r['user_id'], os.environ['SMOKE_PW'])
    print(r['role'], r['email'])
")

journey() {  # role email -> prints the answer body
  local jar="$WORK/$1.jar"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -c "$jar" -H 'Content-Type: application/json' \
    -d "{\"email\": \"$2\", \"password\": \"$USERPW\"}" "$BASE/api/login")
  [ "$code" = "200" ] || fail "$1 signs in ($code)"
  curl -s -b "$jar" -H 'Content-Type: application/json' -H "Idempotency-Key: smoke-$1-$RANDOM$RANDOM" \
    -d '{"question": "total WAC revenue last quarter"}' "$BASE/api/ask"
}
exec_email=$(echo "$emails" | awk '$1=="exec"{print $2}')
ram_email=$(echo "$emails" | awk '$1=="ram"{print $2}')
exec_body=$(journey exec "$exec_email")
echo "$exec_body" | python3 -c "import json,sys; b=json.load(sys.stdin); sys.exit(0 if b['status']=='answered' and '\$' in b['message'] else 1)" \
  && pass "an Exec signs in and gets a priced answer" || fail "Exec journey: $exec_body"
ram_body=$(journey ram "$ram_email")
echo "$ram_body" | python3 -c "import json,sys; b=json.load(sys.stdin); sys.exit(0 if b['status'] in ('answered','denied') and '\$' not in json.dumps(b) else 1)" \
  && pass "a RAM asking for revenue sees no currency anywhere in the response" || fail "RAM journey: $ram_body"
curl -s -o /dev/null -b "$WORK/ram.jar" -c "$WORK/ram.jar" -X POST "$BASE/api/logout"
[ "$(curl -s -o /dev/null -w '%{http_code}' -b "$WORK/ram.jar" "$BASE/api/me")" = "401" ] \
  && pass "after sign-out the session no longer works" || fail "session survived sign-out"

# -- stop -------------------------------------------------------------------
started=$(date +%s)
$CLI stop -t 75 "$APP" >/dev/null
code=$($CLI inspect --format '{{.State.ExitCode}}' "$APP")
[ "$code" = "0" ] && pass "stops cleanly on SIGTERM in $(( $(date +%s) - started )) s (exit 0)" \
  || fail "exit code $code on stop"

summary
