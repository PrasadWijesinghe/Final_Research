# Build context is the REPO ROOT (the workspace lockfile and `shared/` live there):
#   docker build -f services/api/Dockerfile .
# Same pattern as experts/testing/Dockerfile. Used by the end-to-end stack (`make e2e`).

# DEFAULT: Python 3.12.8 on Debian bookworm, pinned (no moving tag).
FROM python:3.12.8-slim-bookworm AS builder

# DEFAULT: uv 0.13.1, the version the lockfile was written with; pinned.
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app
# uv needs every workspace member's pyproject.toml to read the workspace, so the whole
# source tree (minus what .dockerignore removes) is copied, then only this package is installed.
COPY pyproject.toml uv.lock ./
COPY shared ./shared
COPY services ./services
COPY experts ./experts
RUN uv sync --frozen --no-dev --no-editable --package pipelineiq-api

FROM python:3.12.8-slim-bookworm

# Run as a non-root user.
RUN useradd --system --create-home --uid 10001 api
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    API_PORT=8000

# DEFAULT: config/ is copied twice because the code finds it in two ways. /app/config is read via
# ROUTER_RULES_PATH and SANITISE_PATTERNS_FILE (env overrides). shared/file_tags.py has NO env
# override and looks for <site-packages>/config/file_tags.yaml (parent of the installed `shared`),
# so a second copy goes there. The python3.12 path is tied to the pinned base image above.
COPY config /app/config
COPY config /opt/venv/lib/python3.12/site-packages/config
ENV ROUTER_RULES_PATH=/app/config/router_rules.yaml     SANITISE_PATTERNS_FILE=/app/config/sanitise_patterns.yaml

USER api
WORKDIR /home/api
EXPOSE 8000

# Shell form so ${API_PORT} is expanded; `exec` keeps uvicorn as PID 1 for clean shutdown.
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port ${API_PORT}"]
