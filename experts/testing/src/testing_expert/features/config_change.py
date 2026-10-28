"""Test config: did the test runner's own configuration change."""

from __future__ import annotations

from collections.abc import Sequence

from ._types import FeatureDict, FileChange

FEATURE_NAMES = ("tc_test_config_changed", "tc_test_config_files")


def config_features(files: Sequence[FileChange]) -> FeatureDict:
    """`test_config` tag present (jest/vitest/mocha/playwright config, vite config with `test:`)."""
    # DEFAULT: v1 cut, no timeout / workers / environment detail (needs a parsed config diff).
    n = sum(1 for f in files if "test_config" in f.tags)
    return {"tc_test_config_changed": int(n > 0), "tc_test_config_files": n}
