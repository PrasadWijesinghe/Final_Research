"""Checks the docker-compose stack (`make up`): pgvector extension, schemas, Redis."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import psycopg
import pytest
import redis

EXPECTED_SCHEMAS = {"raw", "platform", "dependency", "build", "testing", "infra"}


def _sync_url(url: str) -> str:
    # DATABASE_URL uses the SQLAlchemy form (postgresql+psycopg://); psycopg wants plain postgresql://.
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.mark.integration
def test_postgres_has_vector_extension_and_schemas() -> None:
    with psycopg.connect(_sync_url(os.environ["DATABASE_URL"])) as conn:
        ext = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        rows = conn.execute("SELECT schema_name FROM information_schema.schemata").fetchall()
    schemas = {r[0] for r in rows}
    assert "vector" in ext
    assert EXPECTED_SCHEMAS <= schemas


def test_redis_ping() -> None:
    url = os.environ.get("REDIS_URL", "")
    if not url:
        pytest.skip("REDIS_URL not set")
    parsed = urlparse(url)
    try:
        address = (parsed.hostname or "localhost", parsed.port or 6379)
        with socket.create_connection(address, timeout=1):
            pass
    except OSError:
        pytest.skip("Redis in REDIS_URL is unreachable (run `make up`)")
    assert redis.Redis.from_url(url).ping()
