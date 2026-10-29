"""Change size: how big the commit is and how it splits between source, tests and config."""

from __future__ import annotations

from collections.abc import Sequence

from ._types import FeatureDict, FileChange
from .settings import FeatureSettings

_KEYS = (
    "files_changed",
    "lines_added",
    "lines_deleted",
    "source_files",
    "test_files",
    "config_files",
    "source_lines",
    "test_lines",
    "config_lines",
)
FEATURE_NAMES = tuple(f"cs_{k}" for k in _KEYS)


def change_size(files: Sequence[FileChange], settings: FeatureSettings) -> FeatureDict:
    """Count files and lines, split by the file tags `source`, `test` and any config tag."""
    out: dict[str, int] = dict.fromkeys(_KEYS, 0)
    out["files_changed"] = len(files)
    for f in files:
        lines = f.additions + f.deletions
        out["lines_added"] += f.additions
        out["lines_deleted"] += f.deletions
        if "source" in f.tags:
            out["source_files"] += 1
            out["source_lines"] += lines
        if "test" in f.tags:
            out["test_files"] += 1
            out["test_lines"] += lines
        if f.tags & settings.config_tags:
            out["config_files"] += 1
            out["config_lines"] += lines
    return {f"cs_{k}": v for k, v in out.items()}
