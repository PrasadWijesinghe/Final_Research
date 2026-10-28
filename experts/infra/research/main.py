"""Small HTTP entry point for the self-contained Infra research project."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from model.features import prepare_features
from pydantic import BaseModel, Field

RESEARCH_DIR = Path(__file__).resolve().parent
# DEFAULT: v4-full is the newest locally validated, calibrated research bundle.
DEFAULT_MODEL_VERSION = "v4-full"
# DEFAULT: use the existing Infra Expert port when this standalone API runs by itself.
DEFAULT_PORT = 8104


class PredictionRequest(BaseModel):
    """Raw pre-execution fields accepted by the research model."""

    features: dict[str, Any] = Field(
        description="One workflow run containing the fields documented in model/features.py."
    )


class PredictionResponse(BaseModel):
    """Minimal response a frontend can display."""

    probability: float
    threshold: float
    alert: bool
    model_name: str
    model_version: str


class ModelService:
    """Load one versioned artifact and expose prediction through a small interface."""

    def __init__(self, artifact_path: Path) -> None:
        self.artifact_path = artifact_path
        self._artifact: dict[str, Any] | None = None

    @property
    def model_version(self) -> str:
        return self.artifact_path.parent.name

    def load(self) -> dict[str, Any]:
        if self._artifact is None:
            if not self.artifact_path.is_file():
                raise FileNotFoundError(f"model artifact not found: {self.artifact_path}")
            self._artifact = joblib.load(self.artifact_path)
        return self._artifact

    def predict(self, raw_features: dict[str, Any]) -> PredictionResponse:
        artifact = self.load()
        prepared = prepare_features(pd.DataFrame([raw_features]))
        feature_columns = artifact["feature_columns"]
        predictor = artifact.get("predictor", artifact["pipeline"])
        probability = float(predictor.predict_proba(prepared.loc[:, feature_columns])[0, 1])
        threshold = float(artifact["threshold"])
        return PredictionResponse(
            probability=probability,
            threshold=threshold,
            alert=probability >= threshold,
            model_name=str(artifact["model_name"]),
            model_version=self.model_version,
        )


def create_app(artifact_path: Path | None = None) -> FastAPI:
    """Create the standalone research API."""
    version = os.environ.get("INFRA_MODEL_VERSION", DEFAULT_MODEL_VERSION)
    selected_path = artifact_path or RESEARCH_DIR / "artifacts" / version / "model.joblib"
    service = ModelService(selected_path)
    application = FastAPI(title="Infra research model", version="0.1.0")

    @application.get("/health")
    def health() -> dict[str, Any]:
        try:
            artifact = service.load()
        except (FileNotFoundError, OSError, ValueError) as exc:
            return {"status": "degraded", "reason": str(exc)}
        return {
            "status": "ok",
            "model_name": artifact["model_name"],
            "model_version": service.model_version,
        }

    @application.post("/predict", response_model=PredictionResponse)
    def predict(request: PredictionRequest) -> PredictionResponse:
        try:
            return service.predict(request.features)
        except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=DEFAULT_PORT)
