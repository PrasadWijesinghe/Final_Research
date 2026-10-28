"""Dataset builder: schema on a small synthetic build (rolled-back transaction) and checksums."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from testing_expert.dataset import (
    KEY_COLS,
    TARGET_COLS,
    build_table,
    verify_manifest,
    write_dataset,
)
from testing_expert.features import feature_names

REPO = 987_654_322
T0 = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as c:
        c.execute(
            "insert into raw.repos (id, full_name, default_branch) values (%s, 'ds/synthetic', 'main')",
            (REPO,),
        )
        yield c
        c.rollback()


def add(
    c: psycopg.Connection[Any], n: int, attempt: int, conclusion: str, sub: str, conf: str = "high"
) -> None:
    created = T0 + timedelta(days=n)
    run_id = REPO * 10 + n
    c.execute(
        "insert into raw.workflow_runs (run_id, attempt, repo_id, workflow_path, head_sha, head_branch, event, created_at)"
        " values (%s, %s, %s, 'ci.yml', %s, 'main', 'push', %s)",
        (run_id, attempt, REPO, f"sha{n}", created),
    )
    job_id = run_id * 10 + attempt
    c.execute(
        "insert into raw.jobs (job_id, run_id, attempt, name, job_key, conclusion, started_at, completed_at, runner_os)"
        " values (%s, %s, %s, 'test (20)', 'test', %s, %s, %s, 'Linux')",
        (job_id, run_id, attempt, conclusion, created, created + timedelta(minutes=10)),
    )
    c.execute(
        "insert into testing.labels (run_id, attempt, job_id, sub_label, rule_name, confidence, labelled_by)"
        " values (%s, %s, %s, %s, 'r', %s, 'rule')",
        (run_id, attempt, job_id, sub, conf),
    )


@pytest.mark.integration
def test_schema_and_labels_on_synthetic_build(
    conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    for n in (1, 2, 3):
        add(conn, n, 1, "success", "pass")
    add(conn, 4, 1, "failure", "genuine")  # two failing attempts on sha4 -> strict
    add(conn, 4, 2, "failure", "genuine")
    add(conn, 5, 1, "failure", "genuine")  # single failure -> not strict
    add(conn, 6, 1, "failure", "flaky", "low")
    add(conn, 7, 1, "failure", "unclear")  # excluded
    add(conn, 8, 1, "success", "not_test")  # excluded
    t = build_table(conn, ["ds/synthetic"])
    assert t.num_rows == 7
    assert t.column_names == [*KEY_COLS, *TARGET_COLS, *feature_names()]
    d = t.to_pydict()
    assert set(d["label"]) == {"pass", "genuine_fail", "flaky_fail"}
    strict = {
        (r, a): s
        for r, a, s, lab in zip(
            d["run_id"], d["attempt"], d["genuine_strict"], d["label"], strict=True
        )
        if lab == "genuine_fail"
    }
    assert strict == {(REPO * 10 + 4, 1): True, (REPO * 10 + 4, 2): True, (REPO * 10 + 5, 1): False}
    assert pa.types.is_timestamp(t.schema.field("created_at").type)
    assert d["created_at"][0] == T0 + timedelta(days=1)
    assert pa.types.is_string(t.schema.field("rs_runner_os").type)
    assert d["rs_runner_os"][0] == "Linux"

    out = tmp_path / "v1"
    m = write_dataset(t, out, "v1")
    assert m["rows_per_label"] == {"pass": 3, "genuine_fail": 3, "flaky_fail": 1}
    assert m["variants"] == {"all": 7, "high": 6, "strict": 6}
    assert pq.read_table(out / "rows.parquet").num_rows == 7


def _tiny_table() -> pa.Table:
    names = feature_names()
    cols: dict[str, list[Any]] = {c: [] for c in (*KEY_COLS, *TARGET_COLS, *names)}
    row: dict[str, Any] = {
        "run_id": 1,
        "attempt": 1,
        "job_id": 1,
        "repo": "a/b",
        "created_at": T0,
        "series_key": "test",
        "label": "pass",
        "label_confidence": "high",
        "rule_name": "r",
        "genuine_strict": False,
    }
    for k in cols:
        cols[k].append(row.get(k))
    return pa.table(
        {
            k: pa.array(v) if v[0] is not None else pa.array(v, type=pa.float64())
            for k, v in cols.items()
        }
    )


def test_manifest_checksum_and_overwrite_guard(tmp_path: Path) -> None:
    out = tmp_path / "v1"
    m = write_dataset(_tiny_table(), out, "v1")
    assert m["n_rows"] == 1 and m["feature_names"] == list(feature_names())
    assert json.loads((out / "manifest.json").read_text())["files"]["rows.parquet"]["sha256"]
    assert verify_manifest(out)
    with pytest.raises(FileExistsError):
        write_dataset(_tiny_table(), out, "v1")
    write_dataset(_tiny_table(), out, "v1", force=True)
    (out / "rows.parquet").write_bytes(b"tampered")
    assert not verify_manifest(out)


def test_dataset_feature_names_by_version_and_v2_manifest(tmp_path: Path) -> None:
    from testing_expert.dataset import V2_FROZEN_NOTE, dataset_feature_names

    v1, v2 = dataset_feature_names("v1"), dataset_feature_names("v2")
    assert v2 == list(feature_names()) and len(v2) == len(v1) + 27
    assert not any(n.startswith(("tw_", "h_os_")) for n in v1)
    assert sum(n.startswith("tw_") for n in v2) == 14
    m1 = write_dataset(_tiny_table(), tmp_path / "v1", "v1")
    assert "frozen" not in m1 and "v1_cuts" in m1
    m2 = write_dataset(_tiny_table(), tmp_path / "v2", "v2")
    assert m2["frozen"] is False and m2["frozen_note"] == V2_FROZEN_NOTE
    assert m2["feature_groups_added"] and any("no daily collector" in c for c in m2["cuts"])
    assert m2["rows_per_label"] == {"pass": 1} and m2["git_commit"]
    assert verify_manifest(tmp_path / "v2")
