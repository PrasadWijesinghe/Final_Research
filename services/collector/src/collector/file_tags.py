"""File tagging for the collector: the shared tagger plus the snapshot rule.

The tag logic lives in `shared/file_tags.py` (one implementation, also used by services/features).
The collector adds which tags are worth a content snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from collector.repo_config import DEFAULT_CONFIG_DIR, ConfigError
from shared.file_tags import FileTagConfigError, glob_to_regex
from shared.file_tags import FileTagger as _SharedTagger

__all__ = ["FileTagger", "glob_to_regex", "load_file_tagger"]

# DEFAULT: tags whose files are worth a content snapshot: configuration and manifests. Test files,
# plain source and docs are already visible in the commit diff, so they are not snapshotted.
NON_SNAPSHOT_TAGS = frozenset({"test", "source", "docs", "other"})


@dataclass(frozen=True)
class FileTagger(_SharedTagger):
    """Shared tagger plus `wants_snapshot`."""

    snapshot_exclude: frozenset[str] = field(default=NON_SNAPSHOT_TAGS)

    def wants_snapshot(self, path: str) -> bool:
        """True when `path` carries at least one tag that is worth a content snapshot."""
        return bool(self.tags_for(path) - self.snapshot_exclude)


def load_file_tagger(path: Path | str | None = None) -> FileTagger:
    """Load `config/file_tags.yaml` into a collector FileTagger."""
    try:
        tagger = FileTagger.load(path or DEFAULT_CONFIG_DIR / "file_tags.yaml")
    except FileTagConfigError as exc:
        raise ConfigError(str(exc)) from exc
    assert isinstance(tagger, FileTagger)
    return tagger
