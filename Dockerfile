# Multi-stage build for job-apply-agent
# Stage 1: build with uv
# Stage 2: slim runtime

FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./

# --- dependencies: stable third-party environment keyed only by the lockfile ---
FROM base AS dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra browser

# --- build: add this project without invalidating the dependency/browser layers ---
FROM dependencies AS build
COPY README.md LICENSE ./
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra browser

# --- runtime: minimal image ---
FROM python:3.12-slim AS runtime
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "60";\n' \
        > /etc/apt/apt.conf.d/80-network-retries
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        at-spi2-core ca-certificates curl dbus-x11 xauth xvfb

WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV PLAYWRIGHT_BROWSERS_PATH="/opt/ms-playwright"

# Use Playwright Chromium so the same image builds on Linux amd64 and arm64.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eu; \
    attempt=1; \
    until DEBIAN_FRONTEND=noninteractive playwright install-deps chromium; do \
        if [ "$attempt" -ge 3 ]; then exit 1; fi; \
        attempt=$((attempt + 1)); \
        sleep 2; \
    done
RUN set -eu; \
    attempt=1; \
    until playwright install --no-shell chromium; do \
        if [ "$attempt" -ge 3 ]; then exit 1; fi; \
        attempt=$((attempt + 1)); \
        sleep 2; \
    done; \
    chmod -R a+rX /opt/ms-playwright; \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY --from=build /app/alembic /app/alembic
COPY --from=build /app/alembic.ini .

# Non-root application user. Keep the numeric identity stable because named
# Docker volumes retain ownership across image rebuilds.
RUN groupadd --system --gid 995 appuser \
    && useradd --system --uid 995 --gid appuser --home-dir /home/appuser appuser \
    && mkdir -p /data/artifacts /data/browser-profile /home/appuser \
    && chown -R appuser:appuser /data /home/appuser
USER appuser

EXPOSE 8000
