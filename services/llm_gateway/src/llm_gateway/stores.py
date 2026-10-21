"""Injectable stores: the request cache and the call log (which also gives monthly spend)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from redis import Redis
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from shared.db.models import LlmCall


class CacheStore(Protocol):
    """Key/value cache with a time-to-live."""

    def get(self, key: str) -> str | None:
        """Return the cached text or None."""
        ...

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store text for `ttl_seconds`."""
        ...


class InMemoryCache:
    """Dict-backed cache for tests and single-process use."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        """Return the cached text unless it has expired."""
        item = self._data.get(key)
        if item is None or item[0] < time.monotonic():
            return None
        return item[1]

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store text for `ttl_seconds`."""
        self._data[key] = (time.monotonic() + ttl_seconds, value)


class RedisCache:
    """Redis-backed cache (REDIS_URL)."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    def get(self, key: str) -> str | None:
        """Return the cached text or None."""
        value = self._client.get(f"llm:cache:{key}")
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store text for `ttl_seconds`."""
        self._client.set(f"llm:cache:{key}", value, ex=ttl_seconds)


@dataclass(frozen=True)
class CallRecord:
    """One row of `platform.llm_calls`."""

    expert: str
    model: str
    prompt_version: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    cache_hit: bool
    latency_ms: int
    created_at: datetime


def month_start(now: datetime) -> datetime:
    """First instant of the UTC calendar month containing `now`."""
    now = now.astimezone(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class CallLog(Protocol):
    """Records every call and reports an expert's spend this month."""

    def record(self, call: CallRecord) -> None:
        """Persist one call."""
        ...

    def month_spend_usd(self, expert: str, now: datetime) -> Decimal:
        """Total cost of the expert's calls in the UTC month containing `now`."""
        ...


class InMemoryCallLog:
    """List-backed call log for tests."""

    def __init__(self) -> None:
        self.calls: list[CallRecord] = []

    def record(self, call: CallRecord) -> None:
        """Persist one call."""
        self.calls.append(call)

    def month_spend_usd(self, expert: str, now: datetime) -> Decimal:
        """Total cost of the expert's calls in the month of `now`."""
        start = month_start(now)
        return sum(
            (c.cost_usd for c in self.calls if c.expert == expert and c.created_at >= start),
            Decimal(0),
        )


class SqlCallLog:
    """`platform.llm_calls` in Postgres."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, call: CallRecord) -> None:
        """Insert one row."""
        with Session(self._engine) as session, session.begin():
            session.add(
                LlmCall(
                    created_at=call.created_at,
                    expert=call.expert,
                    model=call.model,
                    prompt_version=call.prompt_version,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cost_usd=call.cost_usd,
                    cache_hit=call.cache_hit,
                    latency_ms=call.latency_ms,
                )
            )

    def month_spend_usd(self, expert: str, now: datetime) -> Decimal:
        """Sum cost_usd for the expert since the start of the month."""
        stmt = select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
            LlmCall.expert == expert, LlmCall.created_at >= month_start(now)
        )
        with Session(self._engine) as session:
            return Decimal(session.execute(stmt).scalar_one())
