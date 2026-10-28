"""Standalone, experimental API for the prototype model; not the expert service API."""

import pickle
from pathlib import Path
from typing import Any

import pandas as pd
import shap
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
MODEL_PATH = REPOSITORY_ROOT / "models/dependency/prototype/xgboost_model.pkl"

with MODEL_PATH.open("rb") as model_file:
    model = pickle.load(model_file)
explainer = shap.TreeExplainer(model)

app = FastAPI(title="Experimental Dependency Prediction API")


class PipelineData(BaseModel):
    """Input features accepted by the experimental, non-contract API."""

    total_dependency_files_changed: int
    has_package_json_change: int
    has_lockfile_change: int


@app.post("/predict")
def predict_failure(data: PipelineData) -> dict[str, Any]:
    """Return the prototype model probability and feature contributions."""
    frame = pd.DataFrame(
        [
            {
                "total_dependency_files_changed": data.total_dependency_files_changed,
                "has_package_json_change": data.has_package_json_change,
                "has_lockfile_change": data.has_lockfile_change,
            }
        ]
    )
    prediction = int(model.predict(frame)[0])
    probability = float(model.predict_proba(frame)[0][1])
    shap_values = explainer.shap_values(frame)[0]
    return {
        "status": "Fail" if prediction == 1 else "Success",
        "failure_probability": f"{probability * 100:.2f}%",
        "shap_data": [
            {"name": name, "impact": float(impact)}
            for name, impact in zip(
                ["Total Files", "package.json", "lockfile"], shap_values, strict=True
            )
        ],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5000)
