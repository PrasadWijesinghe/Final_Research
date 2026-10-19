"""API tests for the testing expert (no network, no Redis)."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from testing_expert.api import app
from testing_expert.config import EXPERT, MODEL_VERSION

from shared.contracts import CommitContext, ReviewDecision, RiskOutput

FIXTURES = Path(__file__).parent / "fixtures"


def _context_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    return data


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _predict(client: TestClient, body: dict[str, Any], **headers: str) -> list[RiskOutput]:
    response = client.post("/predict", json=body, headers=headers)
    assert response.status_code == 200
    return [RiskOutput.model_validate(item) for item in response.json()]


def test_fixture_is_a_valid_commit_context() -> None:
    CommitContext.model_validate(_context_json())


def test_predict_returns_valid_stub_risk_outputs(client: TestClient) -> None:
    outputs = _predict(client, _context_json())
    assert len(outputs) == 1
    out = outputs[0]
    assert out.context_id == UUID("11111111-1111-4111-8111-111111111111")
    assert out.expert == EXPERT
    assert out.expert.value == "test"  # pinned: this folder is the test expert
    # the fixture's single job runs `npm test`, so the output is scoped to that job
    assert out.scope == "job"
    assert (out.workflow_path, out.job_key) == (".github/workflows/ci.yml", "test")
    assert out.status == "ok"
    assert out.model_version == MODEL_VERSION == "stub"
    assert out.threshold == 0.5
    assert out.alert is False
    assert out.probability is not None and 0.01 <= out.probability < 0.2


def test_predict_is_deterministic_per_sha_and_differs_between_shas(client: TestClient) -> None:
    body = _context_json()
    first = _predict(client, body)[0].probability
    assert _predict(client, body)[0].probability == first
    other = {**body, "head_sha": "f" * 40}
    assert _predict(client, other)[0].probability != first


def test_predict_accepts_deadline_header_even_if_garbage(client: TestClient) -> None:
    assert len(_predict(client, _context_json(), **{"X-Deadline-Ms": "10000"})) == 1
    assert len(_predict(client, _context_json(), **{"X-Deadline-Ms": "soon"})) == 1


def test_predict_rejects_invalid_context(client: TestClient) -> None:
    assert client.post("/predict", json={"head_sha": "nope"}).status_code == 422


def test_review_returns_204(client: TestClient) -> None:
    decision = ReviewDecision(
        diagnosis_id=uuid4(),
        decision="accept",
        reviewer_hash="0" * 63 + "2",
        cause_confirmed=True,
    )
    response = client.post("/review", json=decision.model_dump(mode="json"))
    assert response.status_code == 204
    assert response.content == b""


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model_version": "stub", "kb_cases": 0}


def _multi_job_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "commit_context_multi_job.json").read_text("utf-8")
    )
    return data


def test_predict_returns_one_job_scoped_output_per_test_job(client: TestClient) -> None:
    outputs = _predict(client, _multi_job_json())
    assert [(o.scope, o.workflow_path, o.job_key) for o in outputs] == [
        ("job", ".github/workflows/ci.yml", "unit"),
        ("job", ".github/workflows/e2e.yml", "integration"),
    ]
    assert all(o.expert.value == "test" and o.status == "ok" and not o.alert for o in outputs)


def test_predict_falls_back_to_commit_scope_without_test_jobs(client: TestClient) -> None:
    body = _multi_job_json()
    body["workflow_jobs"] = body["workflow_jobs"][:1]  # only the lint job
    outputs = _predict(client, body)
    assert len(outputs) == 1
    assert outputs[0].scope == "commit"
    assert outputs[0].workflow_path is None and outputs[0].job_key is None
    body["workflow_jobs"] = []
    assert _predict(client, body)[0].scope == "commit"
