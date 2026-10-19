"""File tagging from `config/file_tags.yaml` (docs/DATA.md section 5), as the collector needs it.

The collector uses it to decide which changed files get a content snapshot, which are lockfiles
(stored by reference) and how big a patch may be. The full tagger for features is P2.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from collector.repo_config import DEFAULT_CONFIG_DIR, ConfigError

# DEFAULT: tags whose files are worth a content snapshot: configuration and manifests. Test files,
# plain source and docs are already visible in the commit diff, so they are not snapshotted.
NON_SNAPSHOT_TAGS = frozenset({"test", "source", "docs", "other"})


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
    max_patch_bytes: int = 1_048_576
    lockfiles_inline: bool = False
    snapshot_exclude: frozenset[str] = field(default=NON_SNAPSHOT_TAGS)

    def tags_for(self, path: str) -> frozenset[str]:
        """Return every tag matching `path` (`other` when nothing matches)."""
        path = path.lstrip("/")
        direct = {
            name
            for name, rule in self.rules.items()
            if not rule.exclude_tags and any(p.fullmatch(path) for p in rule.patterns)
        }
        for name, rule in self.rules.items():
            if rule.exclude_tags and any(p.fullmatch(path) for p in rule.patterns):
                if not any(t in direct for t in rule.exclude_tags):
                    direct.add(name)
        return frozenset(direct or {"other"})

    def is_lockfile(self, path: str) -> bool:
        """True when `path` is tagged `lockfile`."""
        return "lockfile" in self.tags_for(path)

    def wants_snapshot(self, path: str) -> bool:
        """True when `path` carries at least one tag that is worth a content snapshot."""
        return bool(self.tags_for(path) - self.snapshot_exclude)


def _rule(name: str, raw: Any, path: Path) -> TagRule:
    if isinstance(raw, list):
        patterns, excluded = raw, []
    elif isinstance(raw, dict) and isinstance(raw.get("include"), list):
        patterns, excluded = raw["include"], list(raw.get("exclude_tags") or [])
    else:
        raise ConfigError(f"{path}: tag {name!r} must be a list or {{include, exclude_tags}}")
    return TagRule(tuple(glob_to_regex(str(p)) for p in patterns), tuple(excluded))


def load_file_tagger(path: Path | str | None = None) -> FileTagger:
    """Load `config/file_tags.yaml` into a FileTagger."""
    path = Path(path) if path else DEFAULT_CONFIG_DIR / "file_tags.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot load: {exc}") from exc
    tags = data.get("tags")
    if not isinstance(tags, dict) or not tags:
        raise ConfigError(f"{path}: missing 'tags' mapping")
    limits = data.get("limits") or {}
    return FileTagger(
        rules={name: _rule(name, raw, path) for name, raw in tags.items()},
        max_patch_bytes=int(limits.get("max_patch_bytes", 1_048_576)),
        lockfiles_inline=bool(limits.get("lockfiles_inline", False)),
    )
