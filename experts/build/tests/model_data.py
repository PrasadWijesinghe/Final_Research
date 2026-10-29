"""A synthetic Build dataset for the model tests, in the real rows.parquet layout.

Four repos, commits spread over 90 days. The build fails more often when the last build on the
branch failed (history), when tsconfig/bundler config changed (config) and when many TypeScript
files changed (change); environment features are pure noise. So the model must beat the
last-outcome baseline, and dropping the config group must cost PR-AUC.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from build_expert.dataset.builder import rows_schema
from build_expert.features import CATEGORICAL_FEATURES, FEATURE_NAMES

REPOS = ("acme/web", "acme/api", "beta/ui", "gamma/cli")
START = datetime(2026, 7, 20, tzinfo=UTC)


def make_rows(n: int = 2400, seed: int = 7, synthetic: int = 0) -> list[dict[str, Any]]:
    """`n` commits with a learnable build-failure signal, plus `synthetic` fault-injected ones."""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    for i in range(n + synthetic):
        repo = REPOS[i % len(REPOS)]
        cutoff = START + timedelta(minutes=int(i * 90 * 24 * 60 / (n + synthetic)))
        last_failed = float(rng.random() < 0.15)
        config_changed = float(rng.random() < 0.1)
        ts_files = float(rng.poisson(3))
        logit = -3.0 + 2.2 * last_failed + 2.5 * config_changed + 0.25 * ts_files
        y = int(rng.random() < 1 / (1 + np.exp(-logit)))
        other = int(not y and rng.random() < 0.05)
        feats: dict[str, Any] = {name: float(rng.normal()) for name in FEATURE_NAMES}
        feats.update(
            {
                "package_manager": ["pnpm", "npm", "yarn"][i % 3],
                "branch_last_build_failed": last_failed if i % 10 else None,
                "n_build_config_files": config_changed,
                "n_bundler_config_files": config_changed,
                "n_ts_files": ts_files,
                "last_lint_warnings": float(rng.poisson(4)) if i % 3 == 0 else None,
                "last_type_errors": float(rng.random() < 0.05) if i % 3 == 0 else None,
            }
        )
        for name in CATEGORICAL_FEATURES:
            feats[name] = str(feats[name])
        rows.append(
            {
                "repo": repo,
                "head_sha": f"{i:040x}",
                "branch": "main",
                "event": "push",
                "pr_number": None,
                "author_hash": "a" * 64,
                "history_cutoff": cutoff,
                "completed_at": cutoff + timedelta(minutes=int(rng.integers(5, 60))),
                "n_runs": 1,
                "run_ids": json.dumps([i]),
                "workflow_paths": json.dumps([".github/workflows/ci.yml"]),
                "licence": "MIT",
                "is_synthetic": i >= n,
                "y_build": y,
                "y_build_confident": int(y and rng.random() < 0.8),
                "y_tsc": int(y and ts_files > 3),
                "y_lint": int(y and ts_files <= 3),
                "y_bundler": int(y and config_changed > 0),
                "y_build_other": 0,
                "y_any_failure": int(y or other),
                "y_other_failure": other,
                "n_failed_jobs": int(y or other),
                "n_build_failed_jobs": y,
                "label_confidence": "high" if y else None,
                "label_rules": None,
                "label_status": "ok",
                "top_error_codes": None,
                **feats,
            }
        )
    return rows


def write_dataset(folder: Path, rows: list[dict[str, Any]]) -> Path:
    """Write rows.parquet and a minimal manifest.json; returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=rows_schema()), folder / "rows.parquet")
    manifest = {
        "dataset": f"build/{folder.name}",
        "built_at": "2026-10-28T00:00:00+00:00",
        "files": {"rows.parquet": {"sha256": "0" * 64, "bytes": 1}},
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder
