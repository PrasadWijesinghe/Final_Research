"""Keep the API tests deterministic: no real model bundle and no database unless a test opts in."""

from collections.abc import Iterator

import pytest
from testing_expert import serving

# Only the API and serving tests run in stub mode; DB integration tests keep DATABASE_URL.
_STUB_MODULES = {"test_testing_api", "test_serving"}


@pytest.fixture(autouse=True)
def _stub_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.module.__name__.rsplit(".", 1)[-1] not in _STUB_MODULES:
        yield
        return
    monkeypatch.setenv(serving.BUNDLE_ENV, "/nonexistent/bundle")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    serving.get_bundle.cache_clear()
    yield
    serving.get_bundle.cache_clear()


@pytest.fixture(autouse=True)
def _no_live_knowledge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Diagnose tests never touch the real knowledge base (and never load the embedding model)."""
    from testing_expert import diagnose

    monkeypatch.setattr(diagnose, "retrieve_for_failure", lambda failure, log: [])


@pytest.fixture(autouse=True)
def _no_llm_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches a real LLM gateway; tests that want one set LLM_GATEWAY_URL themselves."""
    monkeypatch.delenv("LLM_GATEWAY_URL", raising=False)
