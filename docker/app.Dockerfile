# IncidentPilot control plane.
#
# The previous build used `pip install .`, which re-resolved the version ranges in
# pyproject.toml on every build, so what got deployed drifted away from the versions
# the test suite actually exercised. This build installs strictly from uv.lock.
#
# uv 0.13.6 is the release whose `uv lock --check` accepts this repo's uv.lock.
# It is bootstrapped from PyPI rather than ghcr.io because registry mirrors that
# front Docker Hub commonly refuse ghcr images, which would break builds here.
ARG UV_VERSION=0.13.6
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS uvbin
ARG UV_VERSION
RUN pip install --no-cache-dir uv==${UV_VERSION}

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

# If this base image moves, rebuild docker/sandbox.Dockerfile too: the sandbox and the
# control plane must stay on the same interpreter release.
COPY --from=uvbin /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    UV_PYTHON_DOWNLOADING=disabled \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/app

# Git and the Docker CLI are runtime dependencies, not build tools: the patch workflow
# diffs against HEAD and the durable worker starts sandbox containers
# (src/incidentpilot/sandbox/manager.py). An image without them degrades silently.
# 65532 is the same uid the sandbox containers drop to.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 incidentpilot \
    && useradd --uid 65532 --gid 65532 --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin incidentpilot \
    && mkdir -p /app \
    && chown 65532:65532 /app

WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}"

COPY --chown=65532:65532 pyproject.toml uv.lock README.md alembic.ini ./
COPY --chown=65532:65532 alembic ./alembic
COPY --chown=65532:65532 src ./src
# The read-only MCP server is spawned as a subprocess of the in-process adapter, so it
# has to be importable from here. reference/ stays out of the image on purpose.
COPY --chown=65532:65532 ops_mcp ./ops_mcp
ENV PYTHONPATH=/app

USER 65532:65532

# Built as the app user so the virtualenv it installs is owned by the app user.
RUN uv sync --frozen --no-dev

# Overridden at build time with `--build-arg GIT_SHA=$(git rev-parse HEAD)`; CI
# re-computes it, so this label cannot go stale unnoticed.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"

# curl is not installed in this image, so the probe uses the venv interpreter.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

EXPOSE 8000

# Default command serves the API; the durable worker overrides it in docker-compose.
CMD ["uvicorn", "incidentpilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
