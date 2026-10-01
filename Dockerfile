# Multi-stage: build the UI with Node, serve everything from one Python image.
# The API and the frontend share an origin in production, so the session cookie
# needs no CORS exemption.

# ---------- stage 1: frontend ----------
FROM node:20-slim AS web
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

# Dependencies first, so application edits do not invalidate the layer.
# Installed from the hashed lock: every package, direct and transitive, at
# the version the suites ran on, and refused if its hash has changed.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app/ ./app/
COPY migrations/ ./migrations/
COPY schema/ ./schema/
COPY scripts/ ./scripts/
COPY pyproject.toml README.md ./
COPY --from=web /build/dist ./web/dist

# Run as a non-root user. Nothing in the image needs to write to it.
RUN useradd --system --uid 10001 --home /app appuser && chown -R appuser /app
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
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", \
     "--timeout-graceful-shutdown", "65"]
