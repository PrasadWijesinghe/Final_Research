"""Root pytest config: skip `integration` tests when the database is unreachable."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

# DEFAULT: Postgres on the standard port when DATABASE_URL has no explicit port.
_DEFAULT_PORT = 5432


def _database_reachable() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return False
    parsed = urlparse(url)
    try:
        # A plain TCP connect is enough to tell "Docker is up" from "not running".
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or _DEFAULT_PORT), timeout=1
        ):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integration = [item for item in items if item.get_closest_marker("integration")]
    if not integration or _database_reachable():
        return
    skip = pytest.mark.skip(reason="database in DATABASE_URL is unreachable (run `make up`)")
    for item in integration:
        item.add_marker(skip)
