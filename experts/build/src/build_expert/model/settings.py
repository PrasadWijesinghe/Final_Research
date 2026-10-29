"""Training settings for the Build model, read from `config/build_model.yaml`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from build_expert.features import FEATURE_GROUPS

KNOWN_TARGETS = ("y_build", "y_build_confident", "y_tsc", "y_lint", "y_bundler", "y_build_other")


@dataclass(frozen=True)
class TrainSettings:
    """Everything `build-train` needs besides the dataset."""

    train_fraction: float = 0.70
    validation_fraction: float = 0.15
    embargo: bool = True
    targets: tuple[str, ...] = ("y_build", "y_build_confident")
    sub_targets: tuple[str, ...] = ("y_tsc", "y_lint", "y_bundler")
    target_precision: float = 0.80
    loro_min_positives: int = 20
    loro_validation_fraction: float = 0.20
    ablation_groups: tuple[str, ...] = ("change", "config", "environment", "history")
    min_positives: int = 5
    top_factors: int = 15
    drop_synthetic: bool = True
    lightgbm: dict[str, Any] = field(default_factory=dict)
    num_boost_round: int = 2000
    early_stopping_rounds: int = 100

    def check(self) -> None:
        """Raise ValueError when a value cannot work."""
        if not 0 < self.train_fraction < 1 or not 0 < self.validation_fraction < 1:
            raise ValueError("split fractions must be between 0 and 1")
        if self.train_fraction + self.validation_fraction >= 1:
            raise ValueError("train + validation must leave something for the test part")
        if not 0 < self.loro_validation_fraction < 1:
            raise ValueError("leave_one_repo_out.validation must be between 0 and 1")
        if not 0 < self.target_precision <= 1:
            raise ValueError("threshold.target_precision must be in (0, 1]")
        for t in (*self.targets, *self.sub_targets):
            if t not in KNOWN_TARGETS:
                raise ValueError(f"unknown target {t!r}; known: {', '.join(KNOWN_TARGETS)}")
        if not self.targets:
            raise ValueError("at least one target is needed")
        for g in self.ablation_groups:
            if g not in FEATURE_GROUPS:
                raise ValueError(f"unknown feature group {g!r}; known: {', '.join(FEATURE_GROUPS)}")


def load_settings(path: Path) -> TrainSettings:
    """Read the YAML file; missing keys keep their defaults."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    split = raw.get("split") or {}
    loro = raw.get("leave_one_repo_out") or {}
    lgb = dict(raw.get("lightgbm") or {})
    defaults = TrainSettings()
    settings = TrainSettings(
        train_fraction=float(split.get("train", defaults.train_fraction)),
        validation_fraction=float(split.get("validation", defaults.validation_fraction)),
        embargo=bool(split.get("embargo", defaults.embargo)),
        targets=tuple(raw.get("targets") or defaults.targets),
        sub_targets=tuple(raw.get("sub_targets") or ()),
        target_precision=float(
            (raw.get("threshold") or {}).get("target_precision", defaults.target_precision)
        ),
        loro_min_positives=int(loro.get("min_positives", defaults.loro_min_positives)),
        loro_validation_fraction=float(loro.get("validation", defaults.loro_validation_fraction)),
        ablation_groups=tuple(raw.get("ablation_groups") or ()),
        min_positives=int(raw.get("min_positives", defaults.min_positives)),
        top_factors=int(raw.get("top_factors", defaults.top_factors)),
        drop_synthetic=bool(raw.get("drop_synthetic", defaults.drop_synthetic)),
        num_boost_round=int(lgb.pop("num_boost_round", defaults.num_boost_round)),
        early_stopping_rounds=int(lgb.pop("early_stopping_rounds", defaults.early_stopping_rounds)),
        lightgbm=lgb,
    )
    settings.check()
    return settings
