"""Write a frozen model bundle to `models/build/<version>/` and load it back for serving.

Layout:
  metrics.json            every result of `run_training`
  report.md               the same, as tables for people
  <target>/model.txt      LightGBM booster (best iteration only)
  <target>/calibrator.json  isotonic calibration points
  <target>/model.json     features, categorical codes, thresholds, dataset and settings
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np

from .data import TrainingData
from .evaluate import TrainingResult
from .report import render_report
from .settings import TrainSettings
from .train import FittedModel

MODEL_FORMAT = 1


def _git_commit(repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=10
        )
    except OSError:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _write_model(
    folder: Path, model: FittedModel, data: TrainingData, meta: dict[str, Any]
) -> None:
    folder.mkdir(parents=True)
    model.booster.save_model(str(folder / "model.txt"), num_iteration=model.best_iteration)
    cal = model.calibrator
    (folder / "calibrator.json").write_text(
        json.dumps({"x": cal.X_thresholds_.tolist(), "y": cal.y_thresholds_.tolist()}),
        encoding="utf-8",
    )
    info = {
        **meta,
        "target": model.target,
        "features": list(model.feature_names),
        "categorical": {k: list(v) for k, v in data.vocab.items() if k in model.feature_names},
        "threshold_f1": model.threshold_f1,
        "threshold_operating": model.threshold_operating,
        "operating_reached_target": model.operating_reached_target,
        "trees": model.best_iteration,
        "scale_pos_weight": model.scale_pos_weight,
        "train_rows": model.train_rows,
        "validation_rows": model.validation_rows,
    }
    (folder / "model.json").write_text(json.dumps(info, indent=2), encoding="utf-8")


def export_bundle(
    out_root: Path,
    version: str,
    result: TrainingResult,
    data: TrainingData,
    settings: TrainSettings,
    *,
    repo_root: Path | None = None,
    clock: Any = lambda: datetime.now(UTC),
) -> Path:
    """Write the bundle; refuse to overwrite an existing version (bundles are frozen)."""
    out_dir = out_root / version
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} already exists; models are frozen, pick a new version")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": f"build/{version}",
        "format": MODEL_FORMAT,
        "trained_at": clock().isoformat(),
        "git_commit": _git_commit(repo_root),
        "lightgbm_version": lgb.__version__,
        "dataset_id": result.results["dataset"].get("id"),
        "dataset_rows_sha256": result.results["dataset"].get("rows_sha256"),
        "settings": asdict(settings),
    }
    for target, model in result.models.items():
        _write_model(out_dir / target, model, data, meta)
    payload = {**meta, **result.results}
    (out_dir / "metrics.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
    return out_dir


class LoadedModel:
    """A bundle's model for one target, ready to score feature rows."""

    def __init__(self, folder: Path) -> None:
        self.info: dict[str, Any] = json.loads((folder / "model.json").read_text(encoding="utf-8"))
        self.booster = lgb.Booster(model_file=str(folder / "model.txt"))
        cal = json.loads((folder / "calibrator.json").read_text(encoding="utf-8"))
        self._cal_x = np.asarray(cal["x"], dtype=float)
        self._cal_y = np.asarray(cal["y"], dtype=float)

    @property
    def features(self) -> list[str]:
        """Feature names in column order."""
        return list(self.info["features"])

    def probabilities(self, x: np.ndarray) -> np.ndarray:
        """Calibrated failure probabilities for a matrix in `features` order."""
        raw = np.asarray(self.booster.predict(x))
        return np.interp(raw, self._cal_x, self._cal_y)
