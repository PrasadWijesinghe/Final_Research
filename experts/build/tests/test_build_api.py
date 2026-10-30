"""API tests for the build expert (no network, no Redis)."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from build_expert.api import app
from build_expert.config import EXPERT, MODEL_VERSION
from fastapi.testclient import TestClient

from shared.contracts import CommitContext, Diagnosis, ReviewDecision, RiskOutput
from shared.contracts.common import FailureCategory

FIXTURES = Path(__file__).parent / "fixtures"


def _context_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    return data


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # No database in these tests: no knowledge base, and no model bundle (the stub answers).
    monkeypatch.setattr("build_expert.api.default_kb", lambda: None)
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
    assert out.expert.value == "build"  # pinned: this folder is the build expert
    assert out.scope == "commit"
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


def test_health_counts_the_knowledge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    class Kb:
        def count(self) -> int:
            return 42

    monkeypatch.setattr("build_expert.api.default_kb", lambda: Kb())
    assert TestClient(app).get("/health").json()["kb_cases"] == 42


# ---- extra endpoints for the dashboard's Build page --------------------------------------------


def test_diagnose_endpoint_runs_the_worker_code(client: TestClient) -> None:
    body = json.loads((FIXTURES / "routed_failure.json").read_text("utf-8"))
    response = client.post("/diagnose", json=body)
    assert response.status_code == 200
    d = Diagnosis.model_validate(response.json())
    # The fixture is an npm ERESOLVE failure: not a build failure, handed to Dependency.
    assert d.status == "misrouted" and d.suggested_category == FailureCategory.DEPENDENCY
    assert client.post("/diagnose", json={"repo": "nope"}).status_code == 422


def test_model_endpoint_without_a_bundle(client: TestClient) -> None:
    assert client.get("/model").json() == {"model_version": "stub", "loaded": False}


def test_examples_are_off_unless_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BUILD_EXAMPLES_DIR", raising=False)
    assert client.get("/examples").json() == []
    assert client.get("/examples/commits/01_a_b_failed.json").status_code == 404


def test_examples_list_and_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "commits").mkdir()
    (tmp_path / "failures").mkdir()
    commit = tmp_path / "commits" / "01_prisma_042d193c89_failed.json"
    commit.write_text(json.dumps(_context_json()), encoding="utf-8")
    (tmp_path / "failures" / "02_langchainjs_tsc.json").write_text("{}", encoding="utf-8")
    (tmp_path / "secret.json").write_text('{"token": "x"}', encoding="utf-8")
    monkeypatch.setenv("BUILD_EXAMPLES_DIR", str(tmp_path))
    assert client.get("/examples").json() == [
        {
            "kind": "commits",
            "name": "01_prisma_042d193c89_failed.json",
            "label": "01 · prisma 042d193c89 · build failed",
        },
        {
            "kind": "failures",
            "name": "02_langchainjs_tsc.json",
            "label": "02 · langchainjs · rules said tsc",
        },
    ]
    got = client.get("/examples/commits/01_prisma_042d193c89_failed.json")
    assert got.status_code == 200 and got.json() == _context_json()
    # Nothing outside the two folders can be read.
    assert client.get("/examples/commits/..%2Fsecret.json").status_code == 404
    assert client.get("/examples/other/secret.json").status_code == 404
    assert client.get("/examples/commits/missing.json").status_code == 404
