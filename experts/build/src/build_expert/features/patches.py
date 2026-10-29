"""Small helpers for reading unified-diff patches line by line."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from shared.contracts import ChangedFile

TS_EXTS = frozenset({".ts", ".tsx", ".mts", ".cts"})
JS_EXTS = frozenset({".js", ".jsx", ".mjs", ".cjs"})
# DEFAULT: folders whose second level is a package in a monorepo (packages/core, apps/web, ...).
MONOREPO_ROOTS = frozenset(
    {
        "packages",
        "apps",
        "libs",
        "modules",
        "services",
        "plugins",
        "examples",
        "tools",
        "extensions",
    }
)


def added_lines(patch: str | None) -> list[str]:
    """Lines a patch adds (without the leading '+'), skipping the '+++' header."""
    if not patch:
        return []
    return [ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def removed_lines(patch: str | None) -> list[str]:
    """Lines a patch removes (without the leading '-'), skipping the '---' header."""
    if not patch:
        return []
    return [ln[1:] for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---")]


def count_matching(lines: Iterable[str], pattern: re.Pattern[str]) -> int:
    """How many lines match the pattern."""
    return sum(1 for ln in lines if pattern.search(ln))


def changed_lines(patch: str | None) -> list[str]:
    """Added and removed lines together."""
    return added_lines(patch) + removed_lines(patch)


def suffix(path: str) -> str:
    """Lower-case file extension; '.d.ts' style double extensions keep only the last part."""
    return PurePosixPath(path).suffix.lower()


def is_declaration_file(path: str) -> bool:
    """True for TypeScript declaration files (*.d.ts, *.d.mts, *.d.cts)."""
    return bool(re.search(r"\.d\.[mc]?ts$", path))


def module_dir(path: str) -> str:
    """The package or top-level folder a file belongs to ('packages/core', 'src', '.')."""
    parts = PurePosixPath(path).parts
    if len(parts) <= 1:
        return "."
    if parts[0] in MONOREPO_ROOTS and len(parts) > 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def module_dirs(files: Iterable[ChangedFile]) -> frozenset[str]:
    """Distinct package or top-level folders touched by the changed files (both rename sides)."""
    dirs: set[str] = set()
    for f in files:
        dirs.add(module_dir(f.path))
        if f.previous_path:
            dirs.add(module_dir(f.previous_path))
    return frozenset(dirs)
