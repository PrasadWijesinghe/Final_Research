"""End to end (P2.9): fixture failure -> router -> Redis queue -> Test worker -> /diagnoses -> API.

Runs against the Compose `e2e` stack (`make e2e`): the test plays the webhook (it calls the
router with the real database and Redis on the host); the worker runs in a container.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from api.failure_queue import queue_name, redis_from_env
from api.router import FailedJob, route_failed_job
from api.schemas import FailureView
from api.store import SqlStore
from rq import Queue
from sqlalchemy import create_engine

from shared.contracts import Diagnosis, Expert, RepoRef
from shared.contracts.routed_failure import FailedStep
from shared.db.session import get_database_url

pytestmark = pytest.mark.e2e

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
# DEFAULT: 60 s for the worker to pick the job up and post (it is a stub, so it takes ~1 s).
TIMEOUT_S = 60.0
POLL_S = 1.0


def test_failure_reaches_diagnosis_through_queue_and_worker(api_url: str) -> None:
    """A test-category failure is routed, diagnosed by the Test worker and visible via the API."""
    cases: list[dict[str, Any]] = json.loads(
        (FIXTURES / "router_failures.json").read_text("utf-8")
    )["cases"]
    case = next(c for c in cases if c["expected_experts"] == ["test"])
    # DEFAULT: ids derived from the clock so repeated runs against one database never collide.
    unique = time.time_ns() % 10**12
    job = FailedJob(
        repo=RepoRef(owner="acme", name="e2e"),
        head_sha="e" * 40,
        run_id=unique,
        job_id=unique + 1,
        job_name="test",
        workflow_path=".github/workflows/ci.yml",
        failed_steps=[
            FailedStep(number=i + 1, name=name, conclusion="failure")
            for i, name in enumerate(case["steps"])
        ],
        log_ref=f"logs/acme-e2e/{unique}/{unique + 1}.log.gz",
        log_excerpt=case["log"],
    )
    store = SqlStore(create_engine(get_database_url()))
    connection = redis_from_env()

    result = route_failed_job(job, store, connection)

    assert result.failure is not None
    failure = result.failure
    assert failure.sent_to == [Expert.TEST]

    # The API already knows the failure (saved before enqueueing); the diagnosis arrives later.
    view: FailureView | None = None
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        response = httpx.get(f"{api_url}/failures/{failure.failure_id}", timeout=5)
        response.raise_for_status()
        view = FailureView.model_validate(response.json())
        if view.diagnoses:
            break
        time.sleep(POLL_S)

    if view is None or not view.diagnoses:
        queue = Queue(queue_name(Expert.TEST), connection=connection)
        rq_job = queue.fetch_job(f"{failure.failure_id}-{Expert.TEST.value}")
        status = rq_job.get_status() if rq_job else "missing"
        pytest.fail(f"no diagnosis within {TIMEOUT_S:.0f} s; RQ job status: {status}")

    assert view.failure.failure_id == failure.failure_id
    assert len(view.diagnoses) == 1
    diagnosis = Diagnosis.model_validate(view.diagnoses[0].model_dump(mode="json"))
    assert diagnosis.failure_id == failure.failure_id
    assert diagnosis.expert == Expert.TEST
