from __future__ import annotations

from pathlib import Path

import pytest
from main import ModelService, create_app


def test_app_exposes_health_and_predict_routes() -> None:
    missing = Path("definitely-missing-model.joblib")
    application = create_app(missing)
    paths = {route.path for route in application.routes}

    assert {"/health", "/predict"} <= paths
    with pytest.raises(FileNotFoundError):
        ModelService(missing).load()
