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
