#!/bin/bash
# Full load into a throwaway postgres:16-alpine container with a given /dev/shm size.
# Usage: shm_load.sh <name> <shm-size or "default">
set -u
NAME=$1 SHM=$2
PORT=$(~/.venvs/pac-release/bin/python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1])")
PW=$(~/.venvs/pac-release/bin/python -c "import secrets; print(secrets.token_hex(16))")
podman rm -f "$NAME" >/dev/null 2>&1
opts=(); [ "$SHM" != "default" ] && opts=(--shm-size "$SHM")
podman run -d --name "$NAME" ${opts[@]+"${opts[@]}"} -e POSTGRES_PASSWORD="$PW" -p 127.0.0.1:$PORT:5432 postgres:16-alpine >/dev/null
for i in $(seq 1 60); do podman exec "$NAME" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
echo "container $NAME shm=$(podman exec "$NAME" df -h /dev/shm | tail -1 | awk '{print $2}') port=$PORT"
cd ~/src/pharma-analytics-copilot
export PAC_DB_HOST=127.0.0.1 PAC_DB_PORT=$PORT PAC_DB_NAME=pac_shm PAC_LLM_PROVIDER=offline AWS_EC2_METADATA_DISABLED=true
export PAC_ADMIN_DSN="postgresql://postgres:$PW@127.0.0.1:$PORT/postgres"
~/.venvs/pac-release/bin/python scripts/bootstrap_db.py --drop --no-env > ~/src/pac-work/final/$NAME-bootstrap.out 2>&1 || { echo "bootstrap failed"; tail -5 ~/src/pac-work/final/$NAME-bootstrap.out; }
~/.venvs/pac-release/bin/python scripts/load_data.py --mode full > ~/src/pac-work/final/$NAME-load.out 2>&1
code=$?
echo "load exit $code"; tail -2 ~/src/pac-work/final/$NAME-load.out | cut -c1-220
podman rm -f "$NAME" >/dev/null 2>&1
exit $code
