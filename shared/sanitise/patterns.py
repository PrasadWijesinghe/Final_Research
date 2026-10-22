"""Loads the custom secret patterns from config/sanitise_patterns.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PATTERNS_ENV = "SANITISE_PATTERNS_FILE"
_FILENAME = "sanitise_patterns.yaml"
_FLAGS = {"ignorecase": re.IGNORECASE, "dotall": re.DOTALL, "multiline": re.MULTILINE}


@dataclass(frozen=True)
class SecretPattern:
    """One compiled custom pattern; `group` limits the redaction to one capture group."""

    type: str
    regex: re.Pattern[str]
    group: str | None = None


def find_patterns_file() -> Path:
    """Locate config/sanitise_patterns.yaml: env override, else walk up from this file."""
    # DEFAULT: SANITISE_PATTERNS_FILE overrides; otherwise search the parents of this file for
    # config/sanitise_patterns.yaml (works from the repo and from an editable install).
    override = os.environ.get(PATTERNS_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / _FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"config/{_FILENAME} not found above {Path(__file__).resolve()}; set {PATTERNS_ENV}"
    )


def parse_patterns(raw: object, source: str) -> tuple[SecretPattern, ...]:
    """Validate and compile the parsed YAML document."""
    entries = raw.get("patterns") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{source}: expected a non-empty 'patterns' list")
    out: list[SecretPattern] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or "type" not in entry or "pattern" not in entry:
            raise ValueError(f"{source}: pattern #{i} needs 'type' and 'pattern'")
        flags = 0
        for name in entry.get("flags", []):
            if name not in _FLAGS:
                raise ValueError(f"{source}: pattern '{entry['type']}' has unknown flag '{name}'")
            flags |= _FLAGS[name]
        try:
            regex = re.compile(str(entry["pattern"]), flags)
        except re.error as exc:
            raise ValueError(f"{source}: pattern '{entry['type']}' is not valid: {exc}") from exc
        group = entry.get("group")
        if group is not None and group not in regex.groupindex:
            raise ValueError(f"{source}: pattern '{entry['type']}' has no group '{group}'")
        out.append(SecretPattern(str(entry["type"]), regex, group))
    return tuple(out)


@lru_cache(maxsize=8)
def _load(path: str) -> tuple[SecretPattern, ...]:
    with open(path, encoding="utf-8") as fh:
        return parse_patterns(yaml.safe_load(fh), path)


def load_patterns(path: Path | str | None = None) -> tuple[SecretPattern, ...]:
    """Load (and cache) the custom patterns from `path` or the default location."""
    return _load(str(path if path is not None else find_patterns_file()))
