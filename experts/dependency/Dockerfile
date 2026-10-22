# Build context is the REPO ROOT (the workspace lockfile and `shared/` live there):
#   docker build -f experts/dependency/Dockerfile .
# The same image runs the API (default CMD) and the diagnose worker, which only changes the
# command:  python -m dependency_expert.worker   (see docker-compose.yml).

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
RUN uv sync --frozen --no-dev --no-editable --package pipelineiq-expert-dependency

FROM python:3.12.8-slim-bookworm

# Run as a non-root user.
RUN useradd --system --create-home --uid 10001 expert
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EXPERT_PORT=8101

USER expert
WORKDIR /home/expert
EXPOSE 8101

# Shell form so ${EXPERT_PORT} is expanded; `exec` keeps uvicorn as PID 1 for clean shutdown.
CMD ["sh", "-c", "exec uvicorn dependency_expert.api:app --host 0.0.0.0 --port ${EXPERT_PORT}"]
