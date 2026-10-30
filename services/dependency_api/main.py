"""Standalone local API for the verified log-only Dependency Expert model."""

from __future__ import annotations

import io
import os
import re
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

import joblib
import pandas as pd
from dependency_expert.features.log_features import (
    LOG_FEATURE_COLUMNS,
    extract_sanitized_log_features,
)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from shared.github import GitHubClient, GitHubConfig, GitHubError
from shared.github.client import GitHubResponseTooLargeError
from shared.sanitise import sanitise

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = REPOSITORY_ROOT / "models/dependency/xgboost_log_only.joblib"
MODEL_PATH = Path(os.environ.get("PIPELINEIQ_DEPENDENCY_MODEL", DEFAULT_MODEL_PATH))
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_ERROR_LINE = re.compile(
    r"error|failed|failure|exception|traceback|ERR_[A-Z0-9_]+|ERESOLVE|"
    r"not found|unsupported engine|peer dependency|integrity|permission denied",
    re.IGNORECASE,
)

app = FastAPI(title="PipelineIQ Dependency Research API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class DependencyPredictionRequest(BaseModel):
    """Accept exactly one raw sanitized-log source or GitHub Actions run URL."""

    log_text: str | None = Field(default=None, max_length=MAX_LOG_BYTES)
    run_url: str | None = None

    @model_validator(mode="after")
    def exactly_one_input(self) -> DependencyPredictionRequest:
        has_log = bool(self.log_text and self.log_text.strip())
        has_url = bool(self.run_url and self.run_url.strip())
        if has_log == has_url:
            raise ValueError("provide exactly one of log_text or run_url")
        return self


class DiagnosticSignal(BaseModel):
    id: str
    label: str
    detected: bool
    description: str


class LogEvidence(BaseModel):
    title: str
    fileName: str
    startLine: int
    excerpt: str
    highlightedLines: list[int]


class Recommendation(BaseModel):
    summary: str
    steps: list[str]


class DependencyAnalysisResponse(BaseModel):
    id: str
    caseName: str
    repository: str
    runId: str
    prediction: Literal["DEPENDENCY_FAILURE", "OTHER_FAILURE"]
    confidence: float
    signals: list[DiagnosticSignal]
    evidence: LogEvidence
    recommendation: Recommendation


@lru_cache(maxsize=1)
def _load_bundle() -> dict[str, Any]:
    if not MODEL_PATH.is_file():
        raise HTTPException(
            status_code=503,
            detail="The log-only model artifact is unavailable. Run the training script first.",
        )
    bundle = joblib.load(MODEL_PATH)
    if not isinstance(bundle, dict) or "model" not in bundle or "feature_columns" not in bundle:
        raise HTTPException(status_code=503, detail="The model artifact has an invalid format.")
    if bundle["feature_columns"] != list(LOG_FEATURE_COLUMNS):
        raise HTTPException(
            status_code=503, detail="The model feature schema does not match this API."
        )
    return bundle


def _parse_run_url(run_url: str) -> tuple[str, str, str]:
    parsed = urlparse(run_url)
    segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or len(segments) < 5
        or segments[2:4] != ["actions", "runs"]
        or not segments[4].isdigit()
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
            for part in segments[:2]
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="run_url must be an HTTPS GitHub Actions run URL.",
        )
    return segments[0], segments[1], segments[4]


def _decode_job_log(payload: bytes, remaining_bytes: int) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            text_files = [entry for entry in archive.infolist() if not entry.is_dir()]
            parts: list[str] = []
            consumed = 0
            for entry in text_files:
                with archive.open(entry) as stream:
                    content = stream.read(max(remaining_bytes - consumed + 1, 1))
                consumed += len(content)
                if consumed > remaining_bytes:
                    raise HTTPException(
                        status_code=413, detail="Combined job logs exceed the 5 MiB limit."
                    )
                parts.append(content.decode("utf-8", errors="replace"))
            return "\n".join(parts)
    except zipfile.BadZipFile:
        if len(payload) > remaining_bytes:
            raise HTTPException(
                status_code=413, detail="Job logs exceed the 5 MiB limit."
            ) from None
        return payload.decode("utf-8", errors="replace")


def _fetch_run_logs(run_url: str) -> tuple[str, str, str, str]:
    owner, repo, run_id = _parse_run_url(run_url)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        # The dashboard's local backend is commonly started from the repo root
        # without shell-exporting its GitHub token. Read only the GitHub token
        # entries from the root .env; never log or return the credential.
        env_file = REPOSITORY_ROOT / ".env"
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                entry = line.strip()
                if entry.startswith("export "):
                    entry = entry[7:].lstrip()
                key, separator, value = entry.partition("=")
                if separator and key.strip() in {"GITHUB_TOKEN", "GH_TOKEN"}:
                    token = value.strip().strip("\"'")
                    if token:
                        break
        except OSError:
            pass
    if not token:
        raise HTTPException(
            status_code=503, detail="Set GITHUB_TOKEN to fetch run logs from GitHub."
        )
    repository = f"{owner}/{repo}"
    run_path = f"/repos/{owner}/{repo}/actions/runs/{run_id}"
    try:
        config = GitHubConfig(timeout=15, max_retries=0)
        with GitHubClient(token=token, config=config) as client:
            run = client.get_json(run_path)
            if run.get("conclusion") != "failure":
                raise HTTPException(
                    status_code=422, detail="The selected Actions run is not a completed failure."
                )
            jobs = list(client.paginate(f"{run_path}/jobs", {"filter": "latest"}, items_key="jobs"))
            failed_jobs = [job for job in jobs if job.get("conclusion") == "failure"]
            if not failed_jobs:
                raise HTTPException(
                    status_code=422, detail="No failed jobs with logs were found for this run."
                )

            log_parts: list[str] = []
            bytes_used = 0
            for job in failed_jobs:
                job_id = job.get("id")
                if not isinstance(job_id, int):
                    raise HTTPException(
                        status_code=502, detail="GitHub returned an invalid failed-job record."
                    )
                log_bytes = client.get_bytes(
                    f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                    max_bytes=MAX_LOG_BYTES,
                )
                if log_bytes is None:
                    raise HTTPException(
                        status_code=503, detail="GitHub no longer has logs for a failed job."
                    )
                part = _decode_job_log(log_bytes, MAX_LOG_BYTES - bytes_used)
                bytes_used += len(part.encode("utf-8"))
                if bytes_used > MAX_LOG_BYTES:
                    raise HTTPException(
                        status_code=413, detail="Combined job logs exceed the 5 MiB limit."
                    )
                log_parts.append(f"Job: {job.get('name', job_id)}\n{part}")
    except HTTPException:
        raise
    except GitHubResponseTooLargeError as error:
        raise HTTPException(
            status_code=413, detail="A failed-job log exceeds the 5 MiB limit."
        ) from error
    except GitHubError as error:
        status = error.status_code if error.status_code in {401, 403, 404, 410} else 502
        raise HTTPException(
            status_code=status, detail="GitHub could not provide this run's logs."
        ) from error
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=502, detail="GitHub returned an invalid run or job response."
        ) from error

    return "\n\n".join(log_parts), repository, f"run {run_id}", f"GitHub Actions run {run_id}"


def _extract_evidence(log_text: str, file_name: str) -> LogEvidence:
    lines = log_text.splitlines()
    matches = [index for index, line in enumerate(lines) if LOG_ERROR_LINE.search(line)]
    anchor = matches[0] if matches else 0
    start = max(0, anchor - 3)
    end = min(len(lines), max(start + 1, anchor + 9))
    selected = lines[start:end]
    highlighted = [index + 1 for index, line in enumerate(selected) if LOG_ERROR_LINE.search(line)]
    excerpt = "\n".join(selected)[:12_000]
    return LogEvidence(
        title="Relevant sanitized log evidence" if matches else "Sanitized failed-job log excerpt",
        fileName=file_name,
        startLine=start + 1,
        excerpt=excerpt,
        highlightedLines=highlighted,
    )


def _signals(features: dict[str, bool]) -> list[DiagnosticSignal]:
    definitions = (
        (
            "install_error",
            "has_install_error",
            "Install / resolution error",
            "A package-manager install or dependency-resolution step reported an error.",
        ),
        (
            "missing_module",
            "has_missing_module",
            "Missing package / module",
            "A required package or module could not be found or imported.",
        ),
        (
            "test_failure",
            "has_test_failure",
            "Test failure",
            "A test command reported one or more failing tests or assertions.",
        ),
        (
            "coverage_error",
            "has_coverage_error",
            "Coverage threshold error",
            "The measured test coverage did not meet a configured threshold.",
        ),
        (
            "lint_type_error",
            "has_lint_type_error",
            "Lint / type-check error",
            "A linter or type checker reported one or more errors.",
        ),
        (
            "test_coverage_lint_error",
            "has_test_coverage_lint_error",
            "Test / coverage / lint (combined)",
            "At least one test, coverage, or lint/type-check indicator was detected.",
        ),
        (
            "ci_permission_error",
            "has_ci_permission_error",
            "CI permission error",
            "The workflow or an action reported missing authorization or permissions.",
        ),
        (
            "external_network_error",
            "has_external_network_error",
            "External network error",
            "A network, DNS, or connection failure was reported in the log.",
        ),
        (
            "peer_dependency_conflict",
            "has_peer_dependency_conflict",
            "Peer dependency conflict",
            "Package versions require incompatible peer dependencies.",
        ),
        (
            "unsupported_toolchain",
            "has_unsupported_engine",
            "Unsupported toolchain / engine",
            "The runtime or package engine is outside the supported range.",
        ),
        (
            "missing_native_bindings",
            "has_missing_native_binding",
            "Missing native bindings",
            "A package could not load a required platform-specific native module.",
        ),
        (
            "build_lifecycle_error",
            "has_build_or_lifecycle_error",
            "Build / lifecycle error",
            "A package install, build, or lifecycle script failed.",
        ),
        (
            "package_integrity_mismatch",
            "has_package_integrity_error",
            "Package integrity mismatch",
            "Downloaded package content did not match the expected integrity value.",
        ),
        (
            "package_manager_cache_miss",
            "has_package_manager_cache_miss",
            "Package-manager cache miss",
            "The log indicates that a package-manager cache or store was not found.",
        ),
    )
    return [
        DiagnosticSignal(id=signal_id, label=label, detected=features[key], description=description)
        for signal_id, key, label, description in definitions
    ]


def _recommendation(features: dict[str, bool], prediction: str) -> Recommendation:
    suggestions: list[str] = []
    if features["has_peer_dependency_conflict"]:
        suggestions.append("Align the conflicting package versions and peer dependency ranges.")
    if features["has_unsupported_engine"]:
        suggestions.append("Use a runtime version supported by the package engine requirements.")
    if features["has_missing_native_binding"]:
        suggestions.append("Reinstall the native package for this runner OS and runtime version.")
    if features["has_package_integrity_error"]:
        suggestions.append(
            "Clear the affected package cache and verify the lockfile integrity entries."
        )
    if features["has_build_or_lifecycle_error"]:
        suggestions.append(
            "Inspect the failing package lifecycle script and its required build tools."
        )
    if features["has_install_error"]:
        suggestions.append("Check the package-manager resolution output and lockfile consistency.")
    if features["has_missing_module"]:
        suggestions.append(
            "Confirm the missing module is declared and installed before the failing step."
        )
    if features["has_external_network_error"]:
        suggestions.append(
            "Check registry or service reachability and retry after the network issue is resolved."
        )
    if features["has_ci_permission_error"]:
        suggestions.append("Review the workflow token permissions required by the failing action.")
    if features["has_test_failure"]:
        suggestions.append(
            "Inspect the failing assertion and confirm its expected value and test fixture."
        )
    if features["has_coverage_error"]:
        suggestions.append(
            "Review the coverage threshold and add tests for the uncovered code path."
        )
    if features["has_lint_type_error"]:
        suggestions.append(
            "Resolve the reported lint or type errors in the referenced source files."
        )
    if not suggestions:
        suggestions.append(
            "Review the highlighted sanitized log lines and identify the first actionable error."
        )
    if prediction == "OTHER_FAILURE" and not features["has_test_failure"]:
        suggestions.insert(
            0, "Confirm the failure is unrelated to dependency installation or package resolution."
        )
    steps = suggestions[:3]
    return Recommendation(
        summary=(
            "The sanitized log contains dependency-related indicators."
            if prediction == "DEPENDENCY_FAILURE"
            else "Log indicators do not strongly support a dependency-related cause."
        ),
        steps=steps,
    )


def _predict_from_log(
    raw_log: str,
    *,
    repository: str,
    run_id: str,
    case_name: str,
    file_name: str,
) -> DependencyAnalysisResponse:
    if len(raw_log.encode("utf-8")) > MAX_LOG_BYTES:
        raise HTTPException(status_code=413, detail="Log text exceeds the 5 MiB limit.")
    cleaned = sanitise(raw_log).text
    features = extract_sanitized_log_features(cleaned)
    bundle = _load_bundle()
    feature_columns = bundle["feature_columns"]
    row = pd.DataFrame(
        [[int(features[column]) for column in feature_columns]], columns=feature_columns
    )
    model = bundle["model"]
    predicted_class = int(model.predict(row)[0])
    probabilities = model.predict_proba(row)[0]
    class_order = [int(label) for label in model.classes_]
    confidence = float(probabilities[class_order.index(predicted_class)])
    prediction = "DEPENDENCY_FAILURE" if predicted_class == 1 else "OTHER_FAILURE"
    return DependencyAnalysisResponse(
        id=str(uuid4()),
        caseName=case_name,
        repository=repository,
        runId=run_id,
        prediction=prediction,
        confidence=confidence,
        signals=_signals(features),
        evidence=_extract_evidence(cleaned, file_name),
        recommendation=_recommendation(features, prediction),
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "model_ready": MODEL_PATH.is_file(), "model_path": str(MODEL_PATH)}


@app.post("/api/v1/predict/dependency", response_model=DependencyAnalysisResponse)
def predict_dependency(request: DependencyPredictionRequest) -> DependencyAnalysisResponse:
    if request.log_text is not None:
        return _predict_from_log(
            request.log_text,
            repository="Uploaded log",
            run_id="local log",
            case_name="Uploaded log analysis",
            file_name="sanitized job log",
        )
    if request.run_url is None:
        raise HTTPException(status_code=422, detail="provide log_text or run_url")
    raw_log, repository, run_id, file_name = _fetch_run_logs(request.run_url)
    return _predict_from_log(
        raw_log,
        repository=repository,
        run_id=run_id,
        case_name="GitHub Actions run analysis",
        file_name=file_name,
    )
