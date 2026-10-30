"""What is real vs fallback (P4.12): readiness summary, /health fields and the worker banner."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from testing_expert import api, status
from testing_expert.api import app


def _patch(
    monkeypatch: pytest.MonkeyPatch, *, model: bool, triage: bool, cases: int, url: str | None
) -> None:
    monkeypatch.setattr(
        status, "get_bundle", lambda: SimpleNamespace(version="lightgbm-v4") if model else None
    )
    monkeypatch.setattr(status, "get_model", lambda: (object(), "triage-v2") if triage else None)
    monkeypatch.setattr(status, "count_cases", lambda: cases)
    monkeypatch.setattr(status, "gateway_url", lambda: url)


@pytest.mark.parametrize(
    ("model", "triage", "mode"),
    [
        (True, True, "real"),
        (True, False, "partial"),
        (False, True, "partial"),
        (False, False, "stub"),
    ],
)
def test_mode_counts_loaded_parts(
    monkeypatch: pytest.MonkeyPatch, model: bool, triage: bool, mode: str
) -> None:
    _patch(monkeypatch, model=model, triage=triage, cases=3, url=None)
    assert status.readiness().mode == mode


def test_readiness_reports_versions_gateway_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, model=True, triage=True, cases=1233, url="http://llm-gateway:8200")
    monkeypatch.setenv("DATA_DIR", "/data")
    state = status.readiness()
    assert (state.model_version, state.triage_version, state.kb_cases) == (
        "lightgbm-v4",
        "triage-v2",
        1233,
    )
    assert state.llm_gateway and state.full_logs


def test_status_endpoint_reports_mode_and_health_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, model=False, triage=False, cases=0, url=None)
    monkeypatch.delenv("DATA_DIR", raising=False)
    # /health reads the real count (and caches it for 30 s) unless patched; keep it out of reach.
    monkeypatch.setattr(api, "count_cases", lambda: 0)
    monkeypatch.setattr(api, "get_bundle", lambda: None)
    body = TestClient(app).get("/status").json()
    assert body == {
        "mode": "stub",
        "model_version": None,
        "triage_version": None,
        "kb_cases": 0,
        "llm_gateway": False,
        "full_logs": False,
    }
    assert set(TestClient(app).get("/health").json()) == {"status", "model_version", "kb_cases"}


def test_readiness_without_loading_never_loads_a_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The worker banner must not load LightGBM before RQ forks (the child would deadlock)."""

    def boom() -> None:
        raise AssertionError("a model was loaded")

    monkeypatch.setattr(status, "get_bundle", boom)
    monkeypatch.setattr(status, "get_model", boom)
    monkeypatch.setattr(status, "count_cases", lambda: 0)
    monkeypatch.setattr(status, "resolve_model_path", lambda: tmp_path / "v4")
    monkeypatch.setattr(status, "resolve_triage_path", lambda: None)
    state = status.readiness(load=False)
    assert state.model_version == "lightgbm-v4"
    assert state.triage_version is None
    assert state.mode == "partial"
