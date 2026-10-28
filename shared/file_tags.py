"""File tagging from `config/file_tags.yaml` (docs/DATA.md section 5): the one implementation.

Used by the collector (which files get a snapshot, lockfiles by reference) and by the features
service (tags on every changed file). Pure: no I/O except reading the YAML in `load`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# DEFAULT: derived from this file's location (repo/shared/file_tags.py); callers can pass a path.
DEFAULT_FILE_TAGS_PATH = Path(__file__).resolve().parents[1] / "config" / "file_tags.yaml"

# vite.config.* is also `test_config` when it has a `test:` block (docs/DATA.md section 5).
_VITE_CONFIG = re.compile(r"(?:.*/)?vite\.config\.[^/]+")
# DEFAULT: a bare `test:` key anywhere (not part of a longer word) counts as a test block.
_TEST_BLOCK = re.compile(r"(?<![\w.])test\s*:")


class FileTagConfigError(ValueError):
    """`file_tags.yaml` is invalid; the message names the file."""


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob (`**` crosses folders; `*` and `?` stay inside one) to a regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


@dataclass(frozen=True)
class TagRule:
    """Patterns for one tag, plus tags that disqualify a file (e.g. `source` excludes `test`)."""

    patterns: tuple[re.Pattern[str], ...]
    exclude_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileTagger:
    """Tags repo-relative paths; each tag is checked independently, `other` only if none match."""

    rules: dict[str, TagRule]
    max_patch_bytes: int = 1_048_576  # DEFAULT: mirrors config/file_tags.yaml limits
    lockfiles_inline: bool = False  # DEFAULT: mirrors config/file_tags.yaml limits

    def tags_for(self, path: str, content: str | None = None) -> frozenset[str]:
        """Return every tag matching `path` (`other` when nothing matches).

        `content` is optional; it is only read to decide if a vite config has a `test:` block.
        """
        path = path.lstrip("/")
        direct = {
            name
            for name, rule in self.rules.items()
            if not rule.exclude_tags and any(p.fullmatch(path) for p in rule.patterns)
        }
        if content and _VITE_CONFIG.fullmatch(path) and _TEST_BLOCK.search(content):
            direct.add("test_config")
        for name, rule in self.rules.items():
            if rule.exclude_tags and any(p.fullmatch(path) for p in rule.patterns):
                if not any(t in direct for t in rule.exclude_tags):
                    direct.add(name)
        return frozenset(direct or {"other"})

    def is_lockfile(self, path: str) -> bool:
        """True when `path` is tagged `lockfile`."""
        return "lockfile" in self.tags_for(path)

    @classmethod
    def load(cls, path: Path | str | None = None) -> FileTagger:
        """Load `config/file_tags.yaml` (or `path`) into a tagger of this class."""
        path = Path(path) if path else DEFAULT_FILE_TAGS_PATH
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise FileTagConfigError(f"{path}: cannot load: {exc}") from exc
        tags = data.get("tags")
        if not isinstance(tags, dict) or not tags:
            raise FileTagConfigError(f"{path}: missing 'tags' mapping")
        # DEFAULT: fallbacks below mirror config/file_tags.yaml limits
        limits = data.get("limits") or {}
        return cls(
            rules={name: _rule(name, raw, path) for name, raw in tags.items()},
            max_patch_bytes=int(limits.get("max_patch_bytes", 1_048_576)),
            lockfiles_inline=bool(limits.get("lockfiles_inline", False)),
        )


def _rule(name: str, raw: Any, path: Path) -> TagRule:
    if isinstance(raw, list):
        patterns, excluded = raw, []
    elif isinstance(raw, dict) and isinstance(raw.get("include"), list):
        patterns, excluded = raw["include"], list(raw.get("exclude_tags") or [])
    else:
        raise FileTagConfigError(
            f"{path}: tag {name!r} must be a list or {{include, exclude_tags}}"
        )
    return TagRule(tuple(glob_to_regex(str(p)) for p in patterns), tuple(excluded))
