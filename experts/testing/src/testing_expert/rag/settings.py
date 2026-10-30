"""Loads config/test_diagnosis.yaml (P4.11) into typed settings, one copy per process."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV = "TEST_DIAGNOSIS_FILE"
_FILENAME = "test_diagnosis.yaml"

# DEFAULT: used when config/test_diagnosis.yaml cannot be found (the slim worker image does not
# bake config/ in). Same patterns as the YAML; the file wins when present.
_FALLBACK_PATTERNS: dict[str, list[tuple[str, float]]] = {
    "dependency": [
        ("ERESOLVE", 3.0),
        ("(?i)unable to resolve dependency tree", 3.0),
        ('(?i)unsupported engine|engine "node" is incompatible', 2.5),
        ("(?i)npm ERR! (code E404|404 Not Found)|ERR_PNPM_", 2.0),
        ("npm ERR!", 1.0),
    ],
    "build": [
        (r"error TS\d{4}", 3.0),
        ("(?i)Module build failed|Failed to compile|Build failed", 2.5),
    ],
    "infra": [
        ("(?i)Resource not accessible by integration", 3.0),
        ("(?i)The runner has received a shutdown signal|lost communication with the server", 3.0),
        ("(?i)No space left on device", 2.5),
        ("(?i)docker: |failed to solve|pull access denied", 2.5),
    ],
}


@dataclass(frozen=True)
class Pattern:
    """A log regex and its weight for one category."""

    regex: re.Pattern[str]
    weight: float


@dataclass(frozen=True)
class DiagnosisSettings:
    """Everything tunable about the diagnosis step."""

    timeout_seconds: float = 20.0
    max_excerpt_chars: int = 3000
    max_tests: int = 3
    max_frames: int = 5
    max_message_chars: int = 700
    max_diff_chars: int = 500
    max_cases: int = 5
    case_text_chars: int = 400
    max_evidence_lines: int = 8
    template_min_similarity: float = 0.6
    misroute_min_score: float = 2.0
    misroute_patterns: dict[str, tuple[Pattern, ...]] = field(
        default_factory=lambda: {
            cat: tuple(Pattern(re.compile(p, re.MULTILINE), w) for p, w in items)
            for cat, items in _FALLBACK_PATTERNS.items()
        }
    )


def find_config_file() -> Path | None:
    """`TEST_DIAGNOSIS_FILE`, else `config/test_diagnosis.yaml` above this file, else None."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / _FILENAME
        if candidate.is_file():
            return candidate
    return None


def parse_config(raw: dict[str, Any]) -> DiagnosisSettings:
    """Build settings from the parsed YAML mapping (missing keys keep the defaults)."""
    d = DiagnosisSettings()
    g, p, m, t = (raw.get(k) or {} for k in ("gateway", "prompt", "misroute", "template"))
    patterns = {
        cat: tuple(
            Pattern(re.compile(item["pattern"], re.MULTILINE), float(item.get("weight", 1.0)))
            for item in items or []
        )
        for cat, items in (m.get("patterns") or {}).items()
    }
    return DiagnosisSettings(
        timeout_seconds=float(g.get("timeout_seconds", d.timeout_seconds)),
        max_excerpt_chars=int(p.get("max_excerpt_chars", d.max_excerpt_chars)),
        max_tests=int(p.get("max_tests", d.max_tests)),
        max_frames=int(p.get("max_frames", d.max_frames)),
        max_message_chars=int(p.get("max_message_chars", d.max_message_chars)),
        max_diff_chars=int(p.get("max_diff_chars", d.max_diff_chars)),
        max_cases=int(p.get("max_cases", d.max_cases)),
        case_text_chars=int(p.get("case_text_chars", d.case_text_chars)),
        max_evidence_lines=int(p.get("max_evidence_lines", d.max_evidence_lines)),
        template_min_similarity=float(t.get("min_similarity", d.template_min_similarity)),
        misroute_min_score=float(m.get("min_score", d.misroute_min_score)),
        misroute_patterns=patterns or d.misroute_patterns,
    )


@lru_cache(maxsize=1)
def load_settings() -> DiagnosisSettings:
    """Settings from the YAML file, or the built-in defaults when the file is missing."""
    path = find_config_file()
    if path is None:
        return DiagnosisSettings()
    return parse_config(yaml.safe_load(path.read_text("utf-8")) or {})
