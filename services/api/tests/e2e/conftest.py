"""E2E guard: skip when the backend API of the Compose e2e stack is unreachable."""

from __future__ import annotations

import os

import httpx
import pytest

# DEFAULT: the compose `api` service publishes port 8000 on the host loopback.
DEFAULT_API_URL = "http://127.0.0.1:8000"


def _skip_or_fail(message: str) -> None:
    """Skip locally; with E2E_REQUIRED=1 (set by `make e2e`) every skip is a failure."""
    if os.environ.get("E2E_REQUIRED") == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def api_url() -> str:
    """Base URL of the running backend; skips (or fails when E2E_REQUIRED=1) if it is not ready."""
    if not os.environ.get("DATABASE_URL"):
        _skip_or_fail("DATABASE_URL is not set")
    url = (os.environ.get("E2E_API_URL", "").strip() or DEFAULT_API_URL).rstrip("/")
    try:
        httpx.get(f"{url}/health", timeout=3).raise_for_status()
    except httpx.HTTPError as exc:
        _skip_or_fail(f"e2e stack not reachable at {url} (run `make e2e`): {exc}")
    return url
