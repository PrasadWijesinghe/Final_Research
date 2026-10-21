"""SqlCallLog against a real Postgres (integration: auto-skipped when the DB is unreachable)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from llm_gateway.stores import CallRecord, SqlCallLog
from sqlalchemy import delete

from shared.db.models import LlmCall
from shared.db.session import get_engine

pytestmark = pytest.mark.integration


def test_sql_call_log_records_and_sums_month_spend() -> None:
    engine = get_engine()
    expert = f"gw-test-{uuid4().hex[:8]}"  # unique so other rows do not interfere
    now = datetime.now(UTC)
    log = SqlCallLog(engine)
    try:
        log.record(CallRecord(expert, "mock-1", "v1", 10, 5, Decimal("0.25"), False, 7, now))
        log.record(CallRecord(expert, "mock-1", "v1", 0, 0, Decimal(0), True, 1, now))
        old = datetime(2000, 1, 1, tzinfo=UTC)
        log.record(CallRecord(expert, "mock-1", "v1", 1, 1, Decimal("9"), False, 1, old))
        assert log.month_spend_usd(expert, now) == Decimal("0.25")
    finally:
        with engine.begin() as conn:
            conn.execute(delete(LlmCall).where(LlmCall.expert == expert))
