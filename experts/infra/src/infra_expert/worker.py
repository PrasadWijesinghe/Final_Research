"""RQ worker for queue `diagnose:<expert>`: RoutedFailure in, Diagnosis out (posted to backend)."""

import logging
import os
from typing import Any

import httpx
from redis import Redis
from rq import Queue, Worker

from shared.contracts import Diagnosis, RoutedFailure

from .config import QUEUE_NAME
from .diagnose import diagnose

logger = logging.getLogger(__name__)

# DEFAULT: 10 s is enough for one small POST; override with BACKEND_TIMEOUT_SECONDS.
_DEFAULT_BACKEND_TIMEOUT_S = 10.0


def process_failure(payload: dict[str, Any]) -> Diagnosis:
    """RQ job: validate the payload, diagnose, post to ${BACKEND_URL}/diagnoses if set."""
    failure = RoutedFailure.model_validate(payload)
    diagnosis = diagnose(failure)
    backend_url = os.environ.get("BACKEND_URL", "").rstrip("/")
    if backend_url:
        timeout = float(os.environ.get("BACKEND_TIMEOUT_SECONDS", _DEFAULT_BACKEND_TIMEOUT_S))
        response = httpx.post(
            f"{backend_url}/diagnoses",
            content=diagnosis.model_dump_json(),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()  # let RQ mark the job failed so it can be retried
        logger.info(
            "diagnosis posted",
            extra={
                "failure_id": str(failure.failure_id),
                "diagnosis_id": str(diagnosis.diagnosis_id),
            },
        )
    return diagnosis


def main() -> None:
    """Connect to REDIS_URL and work the diagnose queue until stopped."""
    logging.basicConfig(level=logging.INFO)
    # DEFAULT: same local Redis as .env.example when REDIS_URL is unset.
    connection = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    Worker([Queue(QUEUE_NAME, connection=connection)], connection=connection).work()


if __name__ == "__main__":
    main()
