"""API tests for the infra expert (no network, no Redis)."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from infra_expert import api
from infra_expert.api import app
from infra_expert.config import EXPERT, MODEL_VERSION
from infra_expert.model import load_model

from shared.contracts import CommitContext, ReviewDecision, RiskOutput

FIXTURES = Path(__file__).parent / "fixtures"


def _context_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    return data


@pytest.fixture()
def client() -> TestClient:
    api._push_history.clear()
    api._latest_push = None
    return TestClient(app)


def _predict(client: TestClient, body: dict[str, Any], **headers: str) -> list[RiskOutput]:
    response = client.post("/predict", json=body, headers=headers)
    assert response.status_code == 200
    return [RiskOutput.model_validate(item) for item in response.json()]


def test_fixture_is_a_valid_commit_context() -> None:
    CommitContext.model_validate(_context_json())


def test_predict_returns_valid_research_risk_outputs(client: TestClient) -> None:
    outputs = _predict(client, _context_json())
    assert len(outputs) == 1
    out = outputs[0]
    assert out.context_id == UUID("11111111-1111-4111-8111-111111111111")
    assert out.expert == EXPERT
    assert out.expert.value == "infra"  # pinned: this folder is the infra expert
    assert out.scope == "commit"
    assert out.status == "degraded"
    assert out.error_message is not None and "Research estimate" in out.error_message
    assert out.model_version == MODEL_VERSION == "v6-public-dataset-research"
    assert out.threshold == pytest.approx(load_model().threshold)
    assert load_model().artifact["model_name"] == "xgboost"
    assert out.probability is not None and 0.0 <= out.probability <= 1.0
    assert out.alert == (out.probability >= out.threshold)


def test_predict_is_deterministic_and_does_not_use_sha_as_a_feature(client: TestClient) -> None:
    body = _context_json()
    first = _predict(client, body)[0].probability
    assert _predict(client, body)[0].probability == first
    other = {**body, "head_sha": "f" * 40}
    assert _predict(client, other)[0].probability == first


def test_predict_accepts_deadline_header_even_if_garbage(client: TestClient) -> None:
    assert len(_predict(client, _context_json(), **{"X-Deadline-Ms": "10000"})) == 1
    assert len(_predict(client, _context_json(), **{"X-Deadline-Ms": "soon"})) == 1


def test_latest_demo_prediction_tracks_the_last_context(client: TestClient) -> None:
    body = {**_context_json(), "event": "push"}
    predicted = RiskOutput.model_validate(client.post("/monitor/predict", json=body).json()[0])
    response = client.get("/demo/latest")
    assert response.status_code == 200
    latest = response.json()
    assert latest["repo"] == "example-org/example-repo"
    assert latest["branch"] == "main"
    assert latest["head_sha"] == _context_json()["head_sha"]
    assert latest["changed_files"] == ["src/index.ts"]
    assert latest["prediction"]["prediction_id"] == str(predicted.prediction_id)
    assert latest["source"] == "push"


def test_manual_prediction_does_not_create_a_live_push(client: TestClient) -> None:
    _predict(client, _context_json())
    assert client.get("/demo/latest").json() is None
    assert client.get("/monitor/history").json() == []


def test_monitor_retries_are_deduplicated(client: TestClient) -> None:
    body = {**_context_json(), "event": "push"}
    first = client.post("/monitor/predict", json=body)
    second = client.post("/monitor/predict", json=body)
    assert first.json() == second.json()
    assert len(client.get("/monitor/history").json()) == 1


def test_monitor_keeps_same_head_with_a_different_diff_base(client: TestClient) -> None:
    body = {**_context_json(), "event": "push"}
    first = client.post("/monitor/predict", json=body)
    second = client.post("/monitor/predict", json={**body, "base_sha": "e" * 40})
    assert first.status_code == second.status_code == 200
    assert first.json()[0]["prediction_id"] != second.json()[0]["prediction_id"]
    assert len(client.get("/monitor/history").json()) == 2


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
    assert response.json() == {
        "status": "degraded",
        "model_version": "v6-public-dataset-research",
        "kb_cases": 0,
    }
