"""Plain-language wording per feature (descriptions.yaml), used later to turn SHAP into text."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_FILE = Path(__file__).with_name("descriptions.yaml")


@lru_cache(maxsize=1)
def load_descriptions() -> dict[str, dict[str, str]]:
    """Return {feature: {"template": ..., "missing": ...}}."""
    raw = yaml.safe_load(_FILE.read_text(encoding="utf-8")) or {}
    return {str(k): {str(a): str(b) for a, b in v.items()} for k, v in raw.items()}


def describe(feature: str, value: object) -> str:
    """Fill the feature's template with `value`; use the `missing` wording when value is None."""
    entry = load_descriptions().get(feature)
    if entry is None:
        return f"{feature} = {value}"
    if value is None:
        return entry.get("missing", f"{feature} is unknown")
    try:
        return entry["template"].format(value=value)
    except (ValueError, KeyError, IndexError):
        return f"{feature} = {value}"
