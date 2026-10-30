"""Read-only helpers for the Build expert's page in the dashboard (`services/dashboard`).

- `model_summary`: the loaded model's version, threshold and test results, read from the
  bundle's `metrics.json`, so the page shows the real numbers instead of typed ones.
- Examples: when `BUILD_EXAMPLES_DIR` names a folder with `commits/` and `failures/` (the viva
  demo kit), the page can list them and load one. Off by default. Only `<kind>/<name>.json` files
  directly in those two folders can be read; any other name is refused.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .serving import MAIN_TARGET, BuildPredictor

EXAMPLE_KINDS = ("commits", "failures")
# A plain file name: letters, digits, "_", "." and "-", ending in .json. No separators, so no
# path outside the examples folder can be named.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}\.json$")
METRICS = ("precision", "recall", "f1", "pr_auc", "roc_auc")


def model_summary(predictor: BuildPredictor | None) -> dict[str, Any]:
    """What the page shows about the model: version, threshold and test results."""
    if predictor is None:
        return {"model_version": "stub", "loaded": False}
    out: dict[str, Any] = {
        "model_version": predictor.version,
        "loaded": True,
        "threshold": predictor.threshold,
        "dataset_id": predictor.main.info.get("dataset_id"),
    }
    path = predictor.bundle_dir / "metrics.json" if predictor.bundle_dir else None
    if path is None or not path.is_file():
        return out
    target: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8")).get("targets", {}).get(MAIN_TARGET, {})
    )
    test = target.get("model", {}).get("test", {}).get("at_f1", {})
    split = target.get("split", {}).get("test", {})
    if test:
        out["test"] = {k: test.get(k) for k in METRICS}
        out["test"]["commits"] = split.get("rows")
        out["test"]["failures"] = split.get("positives")
    baselines = [b for b in target.get("baselines", {}).values() if b.get("test")]
    if baselines:
        best = max(baselines, key=lambda b: b["test"].get("f1") or 0.0)
        out["best_baseline"] = {
            "description": best.get("description"),
            "f1": best["test"].get("f1"),
            "pr_auc": best["test"].get("pr_auc"),
        }
    return out


def examples_dir(env: Mapping[str, str] = os.environ) -> Path | None:
    """The examples folder from BUILD_EXAMPLES_DIR, or None when unset or missing."""
    raw = env.get("BUILD_EXAMPLES_DIR", "").strip()
    return Path(raw) if raw and Path(raw).is_dir() else None


def example_label(kind: str, name: str) -> str:
    """A readable label from the kit's file names.

    `01_prisma_042d193c89_failed.json` -> `01 · prisma 042d193c89 · build failed`;
    `02_langchainjs_tsc.json` -> `02 · langchainjs · rules said tsc`.
    """
    parts = name.removesuffix(".json").split("_")
    if kind == "commits" and len(parts) >= 4:
        repo = "_".join(parts[1:-2])
        return f"{parts[0]} · {repo} {parts[-2]} · build {parts[-1]}"
    if kind == "failures" and len(parts) >= 3:
        repo = "_".join(parts[1:-1])
        return f"{parts[0]} · {repo} · rules said {parts[-1]}"
    return name.removesuffix(".json")


def list_examples(root: Path) -> list[dict[str, str]]:
    """Every example in the folder, commits first, in file-name order."""
    out: list[dict[str, str]] = []
    for kind in EXAMPLE_KINDS:
        folder = root / kind
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            if _NAME.match(f.name):
                out.append({"kind": kind, "name": f.name, "label": example_label(kind, f.name)})
    return out


def read_example(root: Path, kind: str, name: str) -> Any | None:
    """One example's JSON, or None when the kind or name is not allowed or the file is missing."""
    if kind not in EXAMPLE_KINDS or not _NAME.match(name):
        return None
    path = root / kind / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
