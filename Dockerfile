# Multi-stage: build the UI with Node, serve everything from one Python image.
# The API and the frontend share an origin in production, so the session cookie
# needs no CORS exemption.

# ---------- stage 1: frontend ----------
FROM node:24-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------- stage 2: runtime ----------
FROM python:3.13-slim AS runtime

# The source revision this image was built from. Passed by the build
# (`--build-arg PAC_RELEASE=$(git rev-parse HEAD)`), reported by /health and
# with every span, and stamped on the image, so a running container can be
# tied to the commit, the evaluation records and the image digest. "dev"
# means an unreleased local build.
ARG PAC_RELEASE=dev
LABEL org.opencontainers.image.revision=$PAC_RELEASE \
      org.opencontainers.image.source="https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Python puts the SCRIPT's directory on sys.path, not the working
    # directory, so `python scripts/load_data.py` could not import `app`.
    # uvicorn happened to work because it inserts the CWD itself, which hid
    # this until the first real deployment ran a script.
    PYTHONPATH=/app \
    PAC_RELEASE=$PAC_RELEASE

WORKDIR /app

# Debian security updates published since the base image was built. The
# image scan found openssl and libpcre2 one security release behind; the
# fixes were already in the Debian archive, so they are applied here rather
# than waited for in the next base image.
RUN apt-get update \
 && apt-get -y upgrade --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first, so application edits do not invalidate the layer.
# Installed from the hashed lock: every package, direct and transitive, at
# the version the suites ran on, and refused if its hash has changed.
# pip is then removed: nothing runs it after build, and it carries its own
# vendored copies of urllib3, msgpack and pkg_resources, which the scan
# flagged at vulnerable versions (the application's own urllib3 is current).
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
 && python -m pip uninstall -y pip

COPY app/ ./app/
COPY migrations/ ./migrations/
COPY schema/ ./schema/
COPY scripts/ ./scripts/
COPY pyproject.toml README.md ./
COPY --from=web /build/dist ./web/dist

# Run as a non-root user. Nothing in the image needs to write to it.
# And no setuid or setgid file: the application never mounts, switches user or
# changes a password, and a setuid-root mount, umount, su or newgrp (util-linux,
# with open HIGH advisories in the image scan) would be a way from a
# compromised application process to root.
RUN useradd --system --uid 10001 --home /app appuser && chown -R appuser /app \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} +
USER appuser

EXPOSE 8000

# The container is unhealthy until a published dataset exists, so a partially
# loaded refresh never receives traffic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status==200 else 1)"

# On SIGTERM uvicorn lets in-flight requests finish -- for up to 65 s, longer
# than the 60 s request deadline, so a deploy never cuts an answer off
# mid-flight (scripts/drain_check.py). The orchestrator's stop timeout must be
# longer still (compose: stop_grace_period 75s), and it should stop routing
# to the container first: new connections are not refused at the instant of
# the signal.
#
# Logs: one sanitised JSON object per line from uvicorn's first line on
# (app/logs.py) -- no exception text, no query strings, no client addresses.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", \
     "--timeout-graceful-shutdown", "65", "--log-config", "app/log_config.json"]
