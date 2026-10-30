# Build context is the REPO ROOT (the workspace lockfile and `shared/` live there):
#   docker build -f services/llm_gateway/Dockerfile .
# Same pattern as experts/testing/Dockerfile. Runs the gateway on port 8200 (see docker-compose.yml).

# DEFAULT: Python 3.12.8 on Debian bookworm, pinned (no moving tag).
FROM python:3.12.8-slim-bookworm AS builder

# DEFAULT: uv 0.13.1, the version the lockfile was written with; pinned.
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY shared ./shared
COPY services ./services
COPY experts ./experts
RUN uv sync --frozen --no-dev --no-editable --package pipelineiq-llm-gateway

FROM python:3.12.8-slim-bookworm

# Run as a non-root user.
RUN useradd --system --create-home --uid 10001 gateway
COPY --from=builder /opt/venv /opt/venv
# DEFAULT: the gateway's config is baked in, because an installed package cannot find the
# checkout's config/ folder; compose mounts ./config over it so edits need no rebuild.
COPY config/llm.yaml /app/config/llm.yaml
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LLM_GATEWAY_PORT=8200 \
    LLM_CONFIG_FILE=/app/config/llm.yaml

USER gateway
WORKDIR /home/gateway
EXPOSE 8200

# Shell form so ${LLM_GATEWAY_PORT} is expanded; `exec` keeps uvicorn as PID 1 for clean shutdown.
CMD ["sh", "-c", "exec uvicorn llm_gateway.api:app --host 0.0.0.0 --port ${LLM_GATEWAY_PORT}"]
