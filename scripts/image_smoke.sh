#!/usr/bin/env bash
# Build the image and test the artifact itself -- what CI's image job does,
# and further: readiness with data loaded, an authenticated journey per role,
# a clean stop, and a vulnerability scan of the exact image.
#
#   scripts/image_smoke.sh                      # with docker
#   CONTAINER_CLI=podman scripts/image_smoke.sh # with podman
#
# It builds the COMMITTED tree at HEAD; commit first. With PAC_SMOKE_IMAGE set
# it tests that already-built image instead (CI builds once, then runs this).
#
# Everything runs in throwaway containers on a private network: a fresh
# postgres:16-alpine, the image provisioning its own database, a jobs-style
# container loading seed data, and the app container. Passwords are random
# per run and never written to disk. Nothing outside the containers is
# touched; they are removed on exit.
set -euo pipefail

CLI=${CONTAINER_CLI:-docker}
TAG=${PAC_SMOKE_IMAGE:-pharma-analytics-copilot:smoke}
# The architecture to build and run, e.g. linux/amd64 on an arm64 machine
# (emulated). Unset: the engine's native platform.
PLATFORM=${PAC_SMOKE_PLATFORM:-}
plat=(); [ -n "$PLATFORM" ] && plat=(--platform "$PLATFORM")
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
fail() {
  echo "FAIL  $*"; RESULTS+=("FAIL: $*")
  # The container is removed on exit: show why it failed while it exists.
  $CLI logs --tail 40 "$APP" 2>&1 | sed 's/^/  app| /' || true
  summary; exit 1
}
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
if [ -n "${PAC_SMOKE_IMAGE:-}" ]; then
  $CLI image inspect "$TAG" >/dev/null 2>&1 || fail "image $TAG exists"
  pass "testing the prebuilt image $TAG"
else
  mkdir -p "$WORK/context"
  git archive "$RELEASE" | tar -x -C "$WORK/context"
  $CLI build "${plat[@]}" --build-arg PAC_RELEASE="$RELEASE" -t "$TAG" "$WORK/context" > "$WORK/build.log" 2>&1 \
    || { tail -30 "$WORK/build.log"; fail "image builds"; }
  pass "image builds ($(( $(date +%s) - started )) s)"
fi
label=$($CLI image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$TAG")
[ "$label" = "$RELEASE" ] && pass "image is labelled with commit $RELEASE" || fail "revision label is '$label'"
size=$($CLI image inspect --format '{{.Size}}' "$TAG")
pass "image size $(( size / 1000000 )) MB"
# The identity to compare with whatever is deployed. A registry digest exists
# only once the image is pushed; until then the image ID is the artifact.
image_id=$($CLI image inspect --format '{{.Id}}' "$TAG")
arch=$($CLI image inspect --format '{{.Architecture}}' "$TAG")
pass "image id $image_id ($arch)"
if [ -n "$PLATFORM" ]; then
  [ "linux/$arch" = "$PLATFORM" ] && pass "image platform is $PLATFORM" || fail "image is linux/$arch, not $PLATFORM"
fi

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
rc=0
$CLI run --rm "${plat[@]}" --network "$NET" -e PAC_DB_HOST=127.0.0.1 -e PAC_LLM_PROVIDER=offline "$TAG" \
  timeout 25 uvicorn app.api.main:app --log-config app/log_config.json --host 0.0.0.0 --port 8000 > "$WORK/nodb.log" 2>&1 || rc=$?
# Logs carry stable reasons, not exception text (app/logs.py).
python3 scripts/assert_startup_refusal.py "$WORK/nodb.log" "$rc" database_unreachable \
  && pass "refuses to serve with no database" || fail "started without a database"

$CLI run --rm "${plat[@]}" --network "$NET" -e PAC_DB_HOST="$DB" \
  -e PAC_ADMIN_DSN="postgresql://postgres:$SUPER@$DB:5432/postgres" \
  -e PAC_DB_OWNER_PASSWORD="$OWNER" -e PAC_DB_AUTH_PASSWORD="$AUTH" \
  -e PAC_DB_EXEC_PASSWORD="$EXEC" -e PAC_DB_SCOPED_PASSWORD="$SCOPED" \
  "$TAG" python scripts/bootstrap_db.py --drop --no-env > "$WORK/bootstrap.log" 2>&1 \
  && pass "provisions its own database (roles, schema, policies)" \
  || { tail -20 "$WORK/bootstrap.log"; fail "bootstrap from inside the image"; }

rc=0
$CLI run --rm "${plat[@]}" --network "$NET" "${jobs_env[@]}" "$TAG" \
  timeout 25 uvicorn app.api.main:app --log-config app/log_config.json --host 0.0.0.0 --port 8001 > "$WORK/owner.log" 2>&1 || rc=$?
python3 scripts/assert_startup_refusal.py "$WORK/owner.log" "$rc" owner_credential_present \
  && pass "refuses to serve while holding the owner credential" || fail "served with the owner credential"

# -- serving ----------------------------------------------------------------
$CLI run -d "${plat[@]}" --name "$APP" --network "$NET" -p "127.0.0.1:$PORT:8000" "${app_env[@]}" \
  -e PAC_COOKIE_SECURE=false "$TAG" >/dev/null
for _ in $(seq 1 60); do curl -sf "$BASE/health" >/dev/null && break; sleep 1; done
release=$(curl -s "$BASE/health" | python3 -c "import json,sys; print(json.load(sys.stdin)['release'])")
[ "$release" = "$RELEASE" ] && pass "/health reports release $release" || fail "/health release '$release'"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")" = "503" ] \
  && pass "not ready before any data is published" || fail "ready with no data"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")" = "200" ] && pass "UI served from the image" || fail "UI"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/me")" = "401" ] && pass "unauthenticated /api/me is 401" || fail "/api/me"
curl -s -D "$WORK/headers.txt" -o /dev/null -H "X-Request-ID: chosen-by-client" "$BASE/api/me"
grep -qiE "^x-request-id: [0-9a-f]{16}" "$WORK/headers.txt" \
  && pass "responses carry a server-generated X-Request-ID" || fail "X-Request-ID: $(grep -i x-request-id "$WORK/headers.txt")"
user=$($CLI exec "$APP" id -un)
[ "$user" != "root" ] && pass "runs as '$user', not root" || fail "runs as root"

# -- data, through the jobs path --------------------------------------------
$CLI run --rm "${plat[@]}" --network "$NET" "${jobs_env[@]}" "$TAG" \
  python scripts/load_data.py --mode seed > "$WORK/load.log" 2>&1 \
  && pass "seed data loaded by a jobs container" || { tail -20 "$WORK/load.log"; fail "load"; }
for _ in $(seq 1 30); do [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")" = "200" ] && break; sleep 1; done
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")" = "200" ] && pass "ready once a dataset is published" || fail "not ready after load"

$CLI run --rm "${plat[@]}" --network "$NET" "${jobs_env[@]}" -e SMOKE_PW="$USERPW" "$TAG" python -c "
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
" > "$WORK/emails.txt" 2> "$WORK/emails.err" || { cat "$WORK/emails.err"; fail "credentials set by a jobs container"; }
exec_email=$(awk '$1=="exec"{print $2}' "$WORK/emails.txt")
ram_email=$(awk '$1=="ram"{print $2}' "$WORK/emails.txt")
[ -n "$exec_email" ] && [ -n "$ram_email" ] && pass "throwaway credentials set for an Exec and a RAM" \
  || fail "no Exec or RAM to sign in as: $(cat "$WORK/emails.txt")"

# Each step writes to a file and is checked here, in the main shell: a
# failure inside $(...) would be captured and the script would stop silently.
signin() {  # role email
  curl -s -o "$WORK/$1.login" -w '%{http_code}' -c "$WORK/$1.jar" \
    -H 'Content-Type: application/json' \
    -d "{\"email\": \"$2\", \"password\": \"$USERPW\"}" "$BASE/api/login"
}
ask() {  # role question -> body in $WORK/<role>.answer
  curl -s -o "$WORK/$1.answer" -w '%{http_code}' -b "$WORK/$1.jar" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: smoke-$1-$RANDOM$RANDOM" \
    -d "{\"question\": \"$2\"}" "$BASE/api/ask"
}
code=$(signin exec "$exec_email"); [ "$code" = "200" ] && pass "an Exec signs in" \
  || fail "Exec sign-in returned $code: $(cat "$WORK/exec.login")"
code=$(ask exec "total WAC revenue last quarter")
python3 -c "import json,sys; b=json.load(open(sys.argv[1])); sys.exit(0 if b['status']=='answered' and '\$' in b['message'] else 1)" "$WORK/exec.answer" \
  && pass "the Exec gets a priced answer" || fail "Exec answer ($code): $(cat "$WORK/exec.answer")"
code=$(signin ram "$ram_email"); [ "$code" = "200" ] && pass "a RAM signs in" \
  || fail "RAM sign-in returned $code: $(cat "$WORK/ram.login")"
code=$(ask ram "total WAC revenue last quarter")
python3 -c "import json,sys; b=json.load(open(sys.argv[1])); sys.exit(0 if b['status'] in ('answered','denied') and '\$' not in json.dumps(b) else 1)" "$WORK/ram.answer" \
  && pass "the RAM asking for revenue sees no currency anywhere in the response" \
  || fail "RAM answer ($code): $(cat "$WORK/ram.answer")"
curl -s -o /dev/null -b "$WORK/ram.jar" -c "$WORK/ram.jar" -X POST "$BASE/api/logout"
[ "$(curl -s -o /dev/null -w '%{http_code}' -b "$WORK/ram.jar" "$BASE/api/me")" = "401" ] \
  && pass "after sign-out the session no longer works" || fail "session survived sign-out"

# -- logs -------------------------------------------------------------------
# Every line the app wrote, from uvicorn's first: one JSON object each, no
# query string, no password, no client address (app/logs.py).
$CLI logs "$APP" > "$WORK/app.log" 2>&1
python3 - "$WORK/app.log" "$USERPW" <<'PY' && pass "app logs are sanitised JSON lines ($(wc -l < "$WORK/app.log") lines)" || fail "app logs"
import json, sys
lines = [l for l in open(sys.argv[1]).read().splitlines() if l.strip()]
bad = [l for l in lines if not l.startswith("{")]
records = [json.loads(l) for l in lines if l.startswith("{")]
leaks = [l for l in lines if sys.argv[2] in l or "?" in json.dumps([r.get("path") for r in records])]
assert lines and not bad, bad[:3]
assert not leaks, "a log line carries a query string or the password"
assert any(r.get("event") == "http.access" for r in records), "no access lines"
assert all("client" not in r for r in records)
PY

# -- stop -------------------------------------------------------------------
started=$(date +%s)
$CLI stop -t 75 "$APP" >/dev/null
code=$($CLI inspect --format '{{.State.ExitCode}}' "$APP")
[ "$code" = "0" ] && pass "stops cleanly on SIGTERM in $(( $(date +%s) - started )) s (exit 0)" \
  || fail "exit code $code on stop"

summary
