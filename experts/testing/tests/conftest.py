"""Keep the API tests deterministic: no real model bundle and no database unless a test opts in."""

from collections.abc import Iterator

import pytest
from testing_expert import serving


@pytest.fixture(autouse=True)
def _stub_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(serving.BUNDLE_ENV, "/nonexistent/bundle")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    serving.get_bundle.cache_clear()
    yield
    serving.get_bundle.cache_clear()
