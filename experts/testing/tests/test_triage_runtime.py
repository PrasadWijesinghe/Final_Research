"""P4.9 wiring: bundle lookup, triage of a RoutedFailure, Diagnosis from it, stub fallbacks."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from testing_expert import diagnose as diag
from testing_expert import serving
from testing_expert.triage import model as tm
from testing_expert.triage import runtime
from testing_expert.triage.features import FEATURE_NAMES

from shared.contracts import BehaviourDetails, Diagnosis, RoutedFailure

FIXTURES = Path(__file__).parent / "fixtures"
LOG = (FIXTURES / "logs" / "vitest" / "vitejs__vite__94017927800.log").read_text("utf-8")


def tiny_bundle(folder: Path, floor: tuple[float, float]) -> Path:
    """A real (tiny) bundle: flaky exactly when the same job passed earlier on the commit."""
    rng = np.random.default_rng(0)
    df = pd.DataFrame(rng.random((300, len(FEATURE_NAMES))), columns=list(FEATURE_NAMES))
    df["ss_same_job_pass"] = rng.integers(0, 2, 300).astype(float)
    y = df["ss_same_job_pass"].to_numpy().astype(int)
    m = tm.fit_model("logreg", df, y, list(FEATURE_NAMES))
    m.flaky_floor, m.genuine_floor = floor
    tm.save_bundle(m, folder, {}, "tiny")
    return folder


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("BACKEND_URL", raising=False)
    monkeypatch.setenv(runtime.BUNDLE_ENV, "/nonexistent/triage")
    runtime.get_model.cache_clear()


def failure(excerpt: str = LOG[-19_000:]) -> RoutedFailure:
    data: dict[str, Any] = json.loads((FIXTURES / "routed_failure.json").read_text("utf-8"))
    data["log_excerpt"] = excerpt
    return RoutedFailure.model_validate(data)


def use_bundle(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(runtime.BUNDLE_ENV, str(path))
    runtime.get_model.cache_clear()


def test_no_bundle_means_the_stub_answers() -> None:
    assert runtime.triage(failure()) is None
    d = diag.diagnose(failure())
    assert d.root_cause == diag.STUB_ROOT_CAUSE and len(d.evidence) == 1


def test_non_test_log_with_no_known_pattern_keeps_the_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_bundle(tiny_bundle(tmp_path / "v1", (0.9, 0.5)), monkeypatch)
    d = diag.diagnose(failure("something odd happened\nthe step stopped\n"))
    assert d.status == "diagnosed" and d.root_cause == diag.STUB_ROOT_CAUSE
    assert d.evidence[0].text == "the step stopped"


def test_triaged_diagnosis_is_valid_and_carries_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_bundle(tiny_bundle(tmp_path / "v1", (0.9, 0.5)), monkeypatch)
    d = diag.diagnose(failure())
    Diagnosis.model_validate(d.model_dump(mode="json"))
    assert isinstance(d.details, BehaviourDetails)
    assert d.details.verdict in ("genuine", "flaky", "unclear")
    assert d.details.framework == "vitest" and d.details.failing_tests
    assert d.confidence == d.details.verdict_confidence
    assert len(d.evidence) >= 2 and all(e.text for e in d.evidence)
    assert any("assertion" in e.text.lower() for e in d.evidence)
    assert d.root_cause is not None and diag.STUB_ROOT_CAUSE not in d.root_cause  # P4.11 template
    assert d.remediation is not None and d.remediation.summary != diag.STUB_REMEDIATION
    assert d.llm_model is None and d.prompt_version is None  # no gateway in this test


@pytest.mark.parametrize(
    ("floors", "verdict", "rerun"),
    [((0.0, 1.5), "flaky", True), ((1.5, 0.0), "genuine", False), ((1.5, 1.5), "unclear", False)],
)
def test_rerun_is_suggested_only_for_a_flaky_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    floors: tuple[float, float],
    verdict: str,
    rerun: bool,
) -> None:
    use_bundle(tiny_bundle(tmp_path / "v1", floors), monkeypatch)
    d = diag.diagnose(failure())
    assert isinstance(d.details, BehaviourDetails)
    assert (d.details.verdict, d.details.suggest_rerun) == (verdict, rerun)
    assert d.remediation is not None
    assert any("Re-run the failed job" in step for step in d.remediation.steps) is rerun


def test_database_errors_degrade_to_log_only_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_bundle(tiny_bundle(tmp_path / "v1", (0.9, 0.5)), monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    result = runtime.triage(failure())
    assert result is not None and result.evidence.same_sha is None
    assert not result.evidence.overlap.commit_files_known


def test_unreadable_bundle_turns_triage_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tiny_bundle(tmp_path / "v1", (0.9, 0.5))
    (bad / "model.joblib").write_bytes(b"not a model")
    use_bundle(bad, monkeypatch)
    assert runtime.triage(failure()) is None


def test_full_log_is_read_only_from_inside_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text(LOG)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    f = failure("short")
    assert "FAIL" in runtime.log_text(f.model_copy(update={"log_ref": "logs/a.log"}))
    assert runtime.log_text(f.model_copy(update={"log_ref": "../etc/passwd"})) == "short"


# --- the two bundle families must not be confused ---------------------------------------------


def make_pre_push_bundle(folder: Path) -> None:
    folder.mkdir(parents=True)
    for name in ("features.json", "threshold.json", "calibrator.joblib", "model.txt"):
        (folder / name).write_text("x")


def test_triage_folder_is_invisible_to_the_pre_push_bundle_search(tmp_path: Path) -> None:
    root = tmp_path / "models" / "testing"
    make_pre_push_bundle(root / "v2")
    tiny_bundle(root / "triage" / "v7", (0.9, 0.5))  # a higher number, in the other family
    assert serving.resolve_bundle_path(str(root)) == root / "v2"
    assert runtime.resolve_bundle_path(str(root / "triage")) == root / "triage" / "v7"


def test_triage_search_picks_the_newest_version(tmp_path: Path) -> None:
    base = tmp_path / "triage"
    tiny_bundle(base / "v1", (0.9, 0.5))
    tiny_bundle(base / "v10", (0.9, 0.5))
    tiny_bundle(base / "v2", (0.9, 0.5))
    assert runtime.resolve_bundle_path(str(base)) == base / "v10"
    assert runtime.resolve_bundle_path(str(base / "v2")) == base / "v2"
    assert runtime.resolve_bundle_path(str(tmp_path / "none")) is None
