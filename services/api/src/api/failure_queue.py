"""Enqueue RoutedFailures on the per-expert RQ queues `diagnose:<Expert value>`."""

from __future__ import annotations

import logging
import os
from typing import Any

from redis import Redis
from rq import Queue

from shared.contracts import Expert, RoutedFailure

logger = logging.getLogger(__name__)

# DEFAULT: same local Redis as .env.example (and the expert workers) when REDIS_URL is unset.
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

# The job function each expert's worker runs (experts/*/src/*/worker.py). Services never import
# experts, so RQ gets the dotted path as a string and the worker process imports it.
JOB_FUNCTIONS: dict[Expert, str] = {
    Expert.DEPENDENCY: "dependency_expert.worker.process_failure",
    Expert.BUILD: "build_expert.worker.process_failure",
    Expert.TEST: "testing_expert.worker.process_failure",
    Expert.INFRA: "infra_expert.worker.process_failure",
}


def queue_name(expert: Expert) -> str:
    """Queue an expert's worker consumes, e.g. `diagnose:test`."""
    return f"diagnose:{expert.value}"


def redis_from_env() -> Redis:
    """Redis connection from REDIS_URL."""
    return Redis.from_url(os.environ.get("REDIS_URL", "").strip() or DEFAULT_REDIS_URL)


def enqueue_failure(connection: Redis, failure: RoutedFailure) -> list[str]:
    """Put the failure on the queue of each expert in `sent_to`; returns the queue names."""
    payload: dict[str, Any] = failure.model_dump(mode="json")  # the worker re-validates it
    names: list[str] = []
    for expert in dict.fromkeys(failure.sent_to):  # never the same expert twice
        name = queue_name(expert)
        # DEFAULT: job id = failure + expert, so enqueueing the same failure twice while the
        # first job still exists does not create a second job.
        job_id = f"{failure.failure_id}-{expert.value}"
        queue = Queue(name, connection=connection)
        if queue.fetch_job(job_id) is None:
            queue.enqueue(JOB_FUNCTIONS[expert], payload, job_id=job_id)
        names.append(name)
        logger.info(
            "failure enqueued", extra={"failure_id": str(failure.failure_id), "queue": name}
        )
    return names
