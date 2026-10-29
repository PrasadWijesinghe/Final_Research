"""Loads config/test_kb.yaml into typed settings (one copy per process)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV = "TEST_KB_FILE"
TABLE_ENV = "TEST_KB_TABLE"
_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_FILENAME = "test_kb.yaml"

# DEFAULT: used when config/test_kb.yaml cannot be found (for example in the slim worker image,
# which does not mount config/). Same list as the YAML; the file wins when present.
FALLBACK_PERMISSIVE = (
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "CC0-1.0",
    "Zlib",
)  # fmt: skip


@dataclass(frozen=True)
class TagRule:
    """One tag: regexes on the failure text (`log`) and on fix-diff lines (`diff`, `diff_added`)."""

    name: str
    log: tuple[re.Pattern[str], ...] = ()
    diff: tuple[re.Pattern[str], ...] = ()
    diff_added: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class KbSettings:
    """Everything tunable about the knowledge base."""

    permissive_licences: tuple[str, ...] = FALLBACK_PERMISSIVE
    window_days: int = 7
    max_signature_tests: int = 3
    excerpt_chars: int = 1500
    excerpt_files: int = 3
    summary_lines: int = 4
    summary_line_chars: int = 110
    message_chars: int = 240
    max_patch_chars: int = 200_000
    tag_diff_max_lines: int = 80
    embed_max_chars: int = 1500
    embed_batch_size: int = 32
    top_k: int = 5
    candidates: int = 50
    diverse_pool: int = 25
    max_per_fix_commit: int = 1
    rrf_k: int = 60
    max_query_words: int = 40
    max_query_chars: int = 1500
    root_cause_rules: tuple[TagRule, ...] = field(default_factory=tuple)
    fix_rules: tuple[TagRule, ...] = field(default_factory=tuple)


def find_config_file() -> Path | None:
    """`TEST_KB_FILE`, else `config/test_kb.yaml` above this file; None when neither exists."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / _FILENAME
        if candidate.is_file():
            return candidate
    return None


def _rules(raw: dict[str, Any] | None) -> tuple[TagRule, ...]:
    out = []
    for name, spec in (raw or {}).items():
        spec = spec or {}

        def comp(key: str, spec: dict[str, Any] = spec) -> tuple[re.Pattern[str], ...]:
            return tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in spec.get(key, []))

        out.append(TagRule(name, comp("log"), comp("diff"), comp("diff_added")))
    return tuple(out)


def parse_config(raw: dict[str, Any]) -> KbSettings:
    """Build settings from the parsed YAML mapping."""
    m, e, r = raw.get("mining", {}), raw.get("embedding", {}), raw.get("retrieval", {})
    tags = raw.get("tags", {})
    return KbSettings(
        permissive_licences=tuple(raw.get("permissive_licences", FALLBACK_PERMISSIVE)),
        window_days=int(m.get("window_days", 7)),
        max_signature_tests=int(m.get("max_signature_tests", 3)),
        excerpt_chars=int(m.get("excerpt_chars", 1500)),
        excerpt_files=int(m.get("excerpt_files", 3)),
        summary_lines=int(m.get("summary_lines", 4)),
        summary_line_chars=int(m.get("summary_line_chars", 110)),
        message_chars=int(m.get("message_chars", 240)),
        max_patch_chars=int(m.get("max_patch_chars", 200_000)),
        tag_diff_max_lines=int(m.get("tag_diff_max_lines", 80)),
        embed_max_chars=int(e.get("max_chars", 1500)),
        embed_batch_size=int(e.get("batch_size", 32)),
        top_k=int(r.get("top_k", 5)),
        candidates=int(r.get("candidates", 50)),
        diverse_pool=int(r.get("diverse_pool", 25)),
        max_per_fix_commit=int(r.get("max_per_fix_commit", 1)),
        rrf_k=int(r.get("rrf_k", 60)),
        max_query_words=int(r.get("max_query_words", 40)),
        max_query_chars=int(r.get("max_query_chars", 1500)),
        root_cause_rules=_rules(tags.get("root_cause")),
        fix_rules=_rules(tags.get("fix")),
    )


@lru_cache(maxsize=1)
def load_settings() -> KbSettings:
    """Settings from the YAML file, or the built-in defaults when the file is missing."""
    path = find_config_file()
    if path is None:
        return KbSettings()
    return parse_config(yaml.safe_load(path.read_text("utf-8")) or {})


def table() -> str:
    """The cases table, `testing.kb_cases` (TEST_KB_TABLE points tests at a scratch copy)."""
    name = os.environ.get(TABLE_ENV, "testing.kb_cases")
    if not _TABLE_NAME.match(name):
        raise ValueError(f"{TABLE_ENV} must look like schema.table, got {name!r}")
    return name
