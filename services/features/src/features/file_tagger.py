"""Tag changed files and apply the size limits (docs/DATA.md section 5).

The tag rules themselves live in `shared/file_tags.py` (shared with the collector).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from shared.contracts import ChangedFile, FileTag
from shared.file_tags import FileTagger

__all__ = ["RawFile", "TaggedFiles", "load_tagger", "tag_files", "tags_for"]


@dataclass(frozen=True)
class RawFile:
    """A changed file as read from GitHub or a local diff, before tagging."""

    path: str
    status: Literal["added", "modified", "removed", "renamed"]
    additions: int
    deletions: int
    patch: str | None = None
    previous_path: str | None = None
    content: str | None = None  # optional head content, only used for vite.config `test:` blocks


@dataclass(frozen=True)
class TaggedFiles:
    """Tagged files plus whether any patch was dropped for size (context `truncated`)."""

    files: list[ChangedFile]
    truncated: bool


@lru_cache(maxsize=4)
def _load(path: str | None) -> FileTagger:
    return FileTagger.load(path)


def load_tagger(path: Path | str | None = None) -> FileTagger:
    """Load (and cache) the tagger from `config/file_tags.yaml`."""
    return _load(str(path) if path else None)


def tags_for(
    path: str, content: str | None = None, tagger: FileTagger | None = None
) -> list[FileTag]:
    """Return the contract tags for a path, in FileTag declaration order."""
    names = (tagger or load_tagger()).tags_for(path, content)
    return [t for t in FileTag if t.value in names]


def tag_files(files: Iterable[RawFile], tagger: FileTagger | None = None) -> TaggedFiles:
    """Tag every file and enforce the limits.

    A patch over `max_patch_bytes` is dropped (`patch=None`) and `truncated` is set. A lockfile
    patch is stored by reference, never inline (unless `lockfiles_inline`), so it is dropped
    without marking the context truncated.
    """
    tagger = tagger or load_tagger()
    out: list[ChangedFile] = []
    truncated = False
    for f in files:
        tags = tags_for(f.path, f.content, tagger)
        patch = f.patch
        if patch is not None:
            if FileTag.LOCKFILE in tags and not tagger.lockfiles_inline:
                # DEFAULT: lockfile patches are dropped (kept by reference), not marked truncated.
                patch = None
            elif len(patch.encode("utf-8")) > tagger.max_patch_bytes:
                patch = None
                truncated = True
        out.append(
            ChangedFile(
                path=f.path,
                previous_path=f.previous_path,
                status=f.status,
                additions=f.additions,
                deletions=f.deletions,
                patch=patch,
                tags=tags,
            )
        )
    return TaggedFiles(files=out, truncated=truncated)
