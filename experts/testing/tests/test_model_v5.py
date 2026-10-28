"""Model v5 comparison helpers: which columns each config sees."""

from __future__ import annotations

from testing_expert.model.compare_v5 import CONFIGS, config_columns

COLS = ["cs_files_changed", "h_fail_rate_7d", "ht_failed_tests_30d", "ht_touched_fail_rate"]


def test_config_columns_split_the_ht_group() -> None:
    assert config_columns(COLS, "v4") == ["cs_files_changed", "h_fail_rate_7d"]
    assert config_columns(COLS, "v5") == COLS
    assert config_columns(COLS, "v5_job") == [
        "cs_files_changed",
        "h_fail_rate_7d",
        "ht_failed_tests_30d",
    ]
    assert config_columns(COLS, "v5_touched") == [
        "cs_files_changed",
        "h_fail_rate_7d",
        "ht_touched_fail_rate",
    ]
    assert set(CONFIGS) == {"v4", "v5", "v5_job", "v5_touched"}
