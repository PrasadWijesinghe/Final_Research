"""Loads config/test_features.yaml into typed settings (one copy per process)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV = "TEST_FEATURES_FILE"
_FILENAME = "test_features.yaml"


@dataclass(frozen=True)
class FeatureSettings:
    """Every tunable of the pre-run feature pipeline."""

    windows_days: tuple[int, ...]
    rerun_window_days: int
    flip_window: int
    flip_decay: float
    max_consecutive_scan: int
    test_job_window_days: int
    max_job_minutes: int
    test_job_regex: str
    frameworks: tuple[str, ...]
    test_libs: tuple[str, ...]
    config_tags: frozenset[str]

    @property
    def test_job_pattern(self) -> re.Pattern[str]:
        """Compiled, case-insensitive version of `test_job_regex`."""
        return re.compile(self.test_job_regex, re.IGNORECASE)


def find_config_file() -> Path:
    """Locate the YAML: `TEST_FEATURES_FILE`, else `config/` in a parent folder of this file."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / _FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"config/{_FILENAME} not found; set {CONFIG_ENV}")


def load_settings(path: Path | str | None = None) -> FeatureSettings:
    """Read and validate the YAML."""
    file = Path(path) if path else find_config_file()
    raw: dict[str, Any] = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    try:
        hist, repo, deps = raw["history"], raw["repo"], raw["dependencies"]
        return FeatureSettings(
            windows_days=tuple(int(d) for d in hist["windows_days"]),
            rerun_window_days=int(hist["rerun_window_days"]),
            flip_window=int(hist["flip_window"]),
            flip_decay=float(hist["flip_decay"]),
            max_consecutive_scan=int(hist["max_consecutive_scan"]),
            test_job_window_days=int(repo["test_job_window_days"]),
            max_job_minutes=int(repo["max_job_minutes"]),
            test_job_regex=str(repo["test_job_regex"]),
            frameworks=tuple(str(f) for f in repo["frameworks"]),
            test_libs=tuple(str(p) for p in deps["test_libs"]),
            config_tags=frozenset(str(t) for t in raw["config_tags"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{file}: invalid feature settings: {exc!r}") from exc


@lru_cache(maxsize=1)
def get_settings() -> FeatureSettings:
    """Cached settings for the process."""
    return load_settings()
