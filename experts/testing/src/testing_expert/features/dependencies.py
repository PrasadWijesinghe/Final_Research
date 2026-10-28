"""Test dependencies: manifest or lockfile changed, test-library versions bumped."""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase

from ._types import FeatureDict, FileChange
from .settings import FeatureSettings

FEATURE_NAMES = (
    "td_manifest_changed",
    "td_lockfile_changed",
    "td_test_lib_bumped",
    "td_test_lib_added_or_removed",
)

# One dependency line in a unified diff of package.json: `+    "jest": "^29.7.0",`
_DEP_LINE = re.compile(r'^([+-])\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$')


def _dep_changes(patch: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (removed, added) name -> version maps from a package.json patch."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        m = _DEP_LINE.match(line)
        if m:
            (added if m.group(1) == "+" else removed)[m.group(2)] = m.group(3)
    return removed, added


def test_lib_changes(patch: str, settings: FeatureSettings) -> tuple[int, int]:
    """Count test libraries with a changed version, and ones added or removed, in one patch."""
    removed, added = _dep_changes(patch)

    def is_lib(name: str) -> bool:
        return any(fnmatchcase(name, p) for p in settings.test_libs)

    bumped = sum(1 for n in added if n in removed and added[n] != removed[n] and is_lib(n))
    only_one_side = set(added) ^ set(removed)
    moved = sum(1 for n in only_one_side if is_lib(n))
    return bumped, moved


def dependency_features(files: Sequence[FileChange], settings: FeatureSettings) -> FeatureDict:
    """Manifest/lockfile flags plus version bumps of test, mock and assertion libraries."""
    bumped = moved = 0
    for f in files:
        if "package_manifest" in f.tags and f.patch:
            b, m = test_lib_changes(f.patch, settings)
            bumped += b
            moved += m
    return {
        "td_manifest_changed": int(any("package_manifest" in f.tags for f in files)),
        "td_lockfile_changed": int(any("lockfile" in f.tags for f in files)),
        "td_test_lib_bumped": bumped,
        "td_test_lib_added_or_removed": moved,
    }
