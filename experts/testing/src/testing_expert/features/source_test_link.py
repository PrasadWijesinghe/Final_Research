"""Source-test link: did source change without tests changing with it."""

from __future__ import annotations

from collections.abc import Sequence

from ._types import FeatureDict, FileChange

FEATURE_NAMES = (
    "stl_source_without_tests",
    "stl_only_tests_changed",
    "stl_test_to_source_ratio",
)


def source_test_link(files: Sequence[FileChange]) -> FeatureDict:
    """Flags for source without tests and tests-only commits, plus the test/source ratio."""
    source = sum(1 for f in files if "source" in f.tags)
    tests = sum(1 for f in files if "test" in f.tags)
    # DEFAULT: v1 cut, no "test files importing changed modules" (needs tree-sitter / import graph).
    return {
        "stl_source_without_tests": int(source > 0 and tests == 0),
        "stl_only_tests_changed": int(tests > 0 and source == 0),
        "stl_test_to_source_ratio": (tests / source) if source else None,
    }
