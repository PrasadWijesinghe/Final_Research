"""Dataset builder end to end on the in-memory scenario (tests/build_scenario.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import build_scenario as sc
import pyarrow.parquet as pq
import pytest
from build_expert.dataset import (
    FIX_SCHEMA,
    DatasetSettings,
    DatasetSummary,
    ListLabelSink,
    build_dataset,
    rows_schema,
)
from build_expert.features import CATEGORICAL_FEATURES, FEATURE_NAMES

from shared.file_tags import FileTagger

TAGS = Path(__file__).resolve().parents[3] / "config" / "file_tags.yaml"


def _build(
    out: Path, version: str = "v1", sink: ListLabelSink | None = None, **kw: Any
) -> tuple[DatasetSummary, dict[str, dict[str, Any]]]:
    summary = build_dataset(
        sc.source(),
        sc.history(),
        out,
        DatasetSettings(version=version, **kw),
        tagger=FileTagger.load(TAGS),
        label_sink=sink,
    )
    rows = pq.read_table(out / version / "rows.parquet").to_pylist()
    return summary, {r["head_sha"]: r for r in rows}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, DatasetSummary, Any, Any]:
    out = tmp_path_factory.mktemp("datasets")
    sink = ListLabelSink()
    summary, rows = _build(out, sink=sink)
    return out, summary, rows, sink


def test_one_row_per_finished_commit(built: Any) -> None:
    _, summary, rows, _ = built
    assert summary.rows == len(rows) == 9
    assert sc.sha(8) not in rows and sc.sha(9) not in rows
    assert dict(summary.skipped) == {"run_not_completed": 1, "all_runs_cancelled": 1}


@pytest.mark.parametrize(
    ("n", "y_build", "sub", "confidence", "other"),
    [
        (1, 0, None, None, 0),
        (2, 1, "y_tsc", "high", 0),
        (102, 0, None, None, 0),
        (3, 0, None, None, 0),
        (4, 1, "y_lint", "high", 0),
        (5, 0, None, None, 1),
        (6, 1, "y_bundler", "medium", 0),
        (7, 0, None, None, 1),
        (10, 1, "y_lint", "low", 0),
    ],
)
def test_commit_labels(
    built: Any, n: int, y_build: int, sub: str | None, confidence: str | None, other: int
) -> None:
    row = built[2][sc.sha(n)]
    assert row["y_build"] == y_build
    assert row["label_confidence"] == confidence
    assert row["y_other_failure"] == other
    for col in ("y_tsc", "y_lint", "y_bundler", "y_build_other"):
        assert row[col] == int(col == sub)
    assert row["y_build_confident"] == int(confidence in ("high", "medium"))


def test_ids_and_cutoff(built: Any) -> None:
    rows = built[2]
    c2, c4 = rows[sc.sha(2)], rows[sc.sha(4)]
    assert c2["history_cutoff"] == sc.at(11, 9)
    assert c2["completed_at"] == sc.at(11, 9, 10)
    assert (c4["event"], c4["pr_number"], c4["branch"]) == ("pull_request", 12, "feature/x")
    assert c2["licence"] == "MIT" and c2["repo"] == sc.REPO
    assert c2["top_error_codes"].startswith("TS6133")
    assert rows[sc.sha(10)]["label_status"] == "ok"


def test_history_has_no_look_ahead(built: Any) -> None:
    rows = built[2]
    # C2B starts while C2 is still running, so C2's failure is not known to it yet.
    assert rows[sc.sha(102)]["repo_build_fail_rate_7d"] == 0.0
    assert rows[sc.sha(3)]["repo_build_fail_rate_7d"] == pytest.approx(1 / 3)
    assert rows[sc.sha(3)]["branch_last_build_failed"] == 1.0
    assert rows[sc.sha(1)]["repo_build_fail_rate_7d"] is None
    # Warning counts come from the latest earlier run on the branch.
    assert rows[sc.sha(3)]["last_lint_warnings"] == 2.0
    assert rows[sc.sha(3)]["last_type_errors"] == 7.0
    # A pull-request branch with no history of its own falls back to main.
    assert rows[sc.sha(4)]["branch_has_history"] == 0.0
    assert rows[sc.sha(5)]["branch_last_build_failed"] == 1.0


def test_features_are_present_and_typed(built: Any) -> None:
    out = built[0]
    table = pq.read_table(out / "v1" / "rows.parquet")
    assert table.schema.equals(rows_schema())
    row = built[2][sc.sha(2)]
    for name in FEATURE_NAMES:
        assert name in row
        if name not in CATEGORICAL_FEATURES:
            assert row[name] is None or isinstance(row[name], float)
    assert row["tsconfig_strictness_lines"] == 1.0
    assert row["package_manager"] == "pnpm"


def test_fix_candidate_mined(built: Any) -> None:
    table = pq.read_table(built[0] / "v1" / "fix_candidates.parquet")
    assert table.schema.equals(FIX_SCHEMA)
    fixes = table.to_pylist()
    assert len(fixes) == 1
    fix = fixes[0]
    assert (fix["failed_sha"], fix["fix_sha"]) == (sc.sha(2), sc.sha(3))
    assert json.loads(fix["overlap_paths"]) == ["src/api.ts"]
    assert fix["fix_message"] == "fix: remove unused import"
    assert fix["sub_labels"] == "tsc" and fix["hours_to_fix"] == 6.0
    assert json.loads(fix["error_locations"])[0]["code"] == "TS6133"


def test_job_labels_cover_every_attempt(built: Any) -> None:
    sink: ListLabelSink = built[3]
    got = {(lb.job_id, lb.run_attempt, lb.sub_label, lb.confidence) for lb in sink.labels}
    assert got == {
        (500210, 1, "tsc", "high"),
        (500410, 1, "eslint", "high"),
        (500421, 2, "eslint", "high"),
        (500610, 1, "bundler", "medium"),
        (501010, 1, "eslint", "low"),
    }
    assert built[1].labels_written == 5


def test_manifest(built: Any) -> None:
    out = built[0] / "v1"
    m = json.loads((out / "manifest.json").read_text())
    assert m["dataset"] == "build/v1" and m["rows"] == 9
    assert m["label_counts"]["y_build"] == {"0": 5, "1": 4}
    assert m["repos"][sc.REPO]["rows"] == 9
    assert m["skipped_commits"] == {"run_not_completed": 1, "all_runs_cancelled": 1}
    assert m["versions"] == {"label_rules": "1", "parser": "1"}
    assert sum(len(v) for v in m["features"].values()) == len(FEATURE_NAMES)
    for name, info in m["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == info["sha256"]


def test_datasets_are_frozen(built: Any) -> None:
    with pytest.raises(FileExistsError):
        _build(built[0])


def test_window_keeps_earlier_history(tmp_path: Path) -> None:
    summary, rows = _build(tmp_path, start=sc.at(13, 0))
    assert set(rows) == {sc.sha(6), sc.sha(7), sc.sha(10)}
    assert dict(summary.skipped) == {"run_not_completed": 1, "all_runs_cancelled": 1}
    # History still sees the commits before the window.
    assert rows[sc.sha(6)]["repo_build_fail_rate_7d"] == pytest.approx(2 / 6)


def test_relabelling_replaces_earlier_labels(tmp_path: Path) -> None:
    sink = ListLabelSink()
    _build(tmp_path, "a", sink)
    _build(tmp_path, "b", sink)
    assert len(sink.labels) == 5
