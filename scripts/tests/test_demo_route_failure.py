"""Tests for scripts/demo_route_failure.py: routing, waiting for the diagnosis and the summary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import demo_route_failure as demo
import fakeredis
import httpx
import pytest
import respx
from api.failure_queue import queue_name
from api.router import FailedJob
from api.schemas import FailureView
from api.store import InMemoryStore
from rq import Queue

from shared.contracts import (
    BehaviourDetails,
    Diagnosis,
    EvidenceLine,
    Expert,
    Remediation,
    RepoRef,
    RoutedFailure,
)
from shared.contracts.routed_failure import FailedStep

FIXTURES = Path(__file__).resolve().parents[2] / "services" / "api" / "tests" / "fixtures"
API = "http://api.test"


def _test_job() -> FailedJob:
    cases: list[dict[str, Any]] = json.loads(
        (FIXTURES / "router_failures.json").read_text("utf-8")
    )["cases"]
    case = next(c for c in cases if c["expected_experts"] == ["test"])
    return FailedJob(
        repo=RepoRef(owner="vitejs", name="vite"),
        head_sha="a" * 40,
        run_id=1,
        job_id=2,
        job_name="Build&Test: node-24, ubuntu-latest",
        workflow_path=".github/workflows/ci.yml",
        failed_steps=[
            FailedStep(number=i + 1, name=n, conclusion="failure")
            for i, n in enumerate(case["steps"])
        ],
        log_ref="logs/vitejs/vite/1/1/2.log.gz",
        log_excerpt=case["log"],
        replay=True,
    )


def _view(failure: RoutedFailure, with_diagnosis: bool) -> dict[str, Any]:
    diagnoses = []
    if with_diagnosis:
        diagnoses = [
            Diagnosis(
                failure_id=failure.failure_id,
                expert=Expert.TEST,
                status="diagnosed",
                root_cause="Likely flaky: the same test passed on a re-run.",
                confidence=0.7,
                evidence=[EvidenceLine(source="log", text="timeout")],
                remediation=Remediation(summary="Re-run the job."),
                details=BehaviourDetails(
                    verdict="flaky",
                    verdict_confidence=0.7,
                    framework="vitest",
                    failing_tests=["a > b"],
                    stack_touches_changed_code=False,
                    suggest_rerun=True,
                ),
                llm_model="mock-1 (placeholder answers, text from template)",
                latency_ms=5,
            ).model_dump(mode="json")
        ]
    return {
        "failure": failure.model_dump(mode="json"),
        "diagnoses": diagnoses,
        "reviews": [],
    }


def test_route_one_saves_the_failure_and_queues_it_for_the_test_worker() -> None:
    store, connection = InMemoryStore(), fakeredis.FakeRedis()
    result = demo.route_one(_test_job(), store, connection)
    assert result.failure is not None
    assert result.failure.replay is True  # a demo never touches GitHub
    assert store.failures[result.failure.failure_id] == result.failure
    queue = Queue(queue_name(Expert.TEST), connection=connection)
    assert queue.fetch_job(f"{result.failure.failure_id}-test") is not None


def test_wait_returns_the_view_once_a_diagnosis_exists() -> None:
    failure = demo.route_one(_test_job(), InMemoryStore(), fakeredis.FakeRedis()).failure
    assert failure is not None
    url = f"{API}/failures/{failure.failure_id}"
    with respx.mock() as mock:
        mock.get(url).mock(
            side_effect=[
                httpx.Response(200, json=_view(failure, False)),
                httpx.Response(200, json=_view(failure, True)),
            ]
        )
        view = demo.wait_for_diagnosis(API, str(failure.failure_id), timeout_s=5, poll_s=0)
    assert view is not None
    assert len(view.diagnoses) == 1


def test_wait_gives_up_with_none() -> None:
    failure = demo.route_one(_test_job(), InMemoryStore(), fakeredis.FakeRedis()).failure
    assert failure is not None
    with respx.mock() as mock:
        mock.get(f"{API}/failures/{failure.failure_id}").mock(
            return_value=httpx.Response(200, json=_view(failure, False))
        )
        assert demo.wait_for_diagnosis(API, str(failure.failure_id), timeout_s=0, poll_s=0) is None


def test_summary_names_verdict_cases_and_text_source() -> None:
    failure = demo.route_one(_test_job(), InMemoryStore(), fakeredis.FakeRedis()).failure
    assert failure is not None
    view = FailureView.model_validate(_view(failure, True))
    text = "\n".join(demo.summarise(view))
    assert "verdict         flaky (confidence 0.70)" in text
    assert "retrieved cases 0" in text
    assert "mock-1" in text
    assert str(failure.failure_id) in text


def test_cli_needs_job_id_or_list(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        demo.main([])
    assert exc.value.code == 2
    assert "--list" in capsys.readouterr().err
