"""Tests for the P4.2 extraction: row building, one-log reading, and the DB round trip."""

from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from testing_expert.extract import (
    REASON_SEED,
    REASON_SIBLING,
    JobRef,
    build_rows,
    extract_one,
    is_test_job,
    make_test_id,
    run_stage,
)
from testing_expert.parsing.models import CaseResult, FailedTest, ParsedLog

JEST_FAIL = """\
FAIL src/a.test.js
  ● math › adds

    expect(received).toBe(expected)

    Expected: 3
    Received: 2

      at Object.<anonymous> (src/a.test.js:4:20)

Tests:       1 failed, 1 total
Test Suites: 1 failed, 1 total
"""


def _failed(name: str, file: str | None = "a.js", kind: str = "test") -> FailedTest:
    return FailedTest(
        full_name=name,
        name_parts=(name,),
        file=file,
        file_source="header",
        error_type=None,
        message="boom",
        kind=kind,  # type: ignore[arg-type]
    )


def test_test_id_is_stable_and_depends_on_every_part() -> None:
    a = make_test_id("o/r", "a.js", "x")
    assert a == make_test_id("o/r", "a.js", "x")
    assert (
        len(
            {
                a,
                make_test_id("o/q", "a.js", "x"),
                make_test_id("o/r", "b.js", "x"),
                make_test_id("o/r", "a.js", "y"),
            }
        )
        == 4
    )


def test_build_rows_failure_overrides_pass_and_dedupes() -> None:
    parsed = ParsedLog(
        framework="jest",
        granularity="test",
        results=(
            CaseResult("t1", "a.js", "passed", 5.0),
            CaseResult("t1", "a.js", "passed", 5.0),
            CaseResult("t2", "a.js", "passed"),
        ),
        failures=(_failed("t1"),),
    )
    rows = {r.full_name: r for r in build_rows("o/r", parsed)}
    assert set(rows) == {"t1", "t2"}
    assert rows["t1"].outcome == "failed"
    assert rows["t1"].granularity == "test"


def test_build_rows_file_granularity_and_suite_failure() -> None:
    parsed = ParsedLog(
        framework="mocha",
        granularity="file",
        results=(CaseResult("a.js", "a.js", "passed"),),
        failures=(_failed("Test suite failed to run", "b.js", kind="suite"),),
    )
    rows = build_rows("o/r", parsed)
    assert {(r.file, r.full_name, r.granularity) for r in rows} == {
        ("a.js", "", "file"),
        ("b.js", "", "file"),
    }


def test_build_rows_skips_unhandled_errors() -> None:
    parsed = ParsedLog(framework="vitest", failures=(_failed("err", kind="unhandled"),))
    assert build_rows("o/r", parsed) == ()


def test_is_test_job() -> None:
    assert not is_test_job(ParsedLog(framework="other"))
    assert is_test_job(ParsedLog(framework="jest"))


def _ref(path: str, reason: str = REASON_SEED, job_id: int = 1) -> JobRef:
    return JobRef(job_id, 10, 1, "o/r", "abc", "test (20)", path, reason)


def test_extract_one_missing_file(tmp_path: Path) -> None:
    out = extract_one((_ref("logs/none.gz"), str(tmp_path)))
    assert out.error == "missing_file"
    assert not out.is_test_job
    assert out.rows == ()


def test_extract_one_jest_log(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "j.log.gz").write_bytes(gzip.compress(JEST_FAIL.encode()))
    out = extract_one((_ref("logs/j.log.gz"), str(tmp_path)))
    assert out.framework == "jest"
    assert out.is_test_job
    # jest prints file-level results, plus the failing test itself from its error block
    assert {(r.granularity, r.outcome) for r in out.rows} == {
        ("file", "failed"),
        ("test", "failed"),
    }


def test_extract_one_not_a_test_log(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "l.log.gz").write_bytes(gzip.compress(b"npm run lint\nall good\n"))
    out = extract_one((_ref("logs/l.log.gz"), str(tmp_path)))
    assert not out.is_test_job
    assert out.rows == ()


@pytest.mark.integration
def test_write_is_idempotent_and_replaces_rows(tmp_path: Path) -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "j.log.gz").write_bytes(gzip.compress(JEST_FAIL.encode()))
    job_id = 987_000_001
    refs = [
        _ref("logs/j.log.gz", REASON_SEED, job_id),
        _ref("logs/missing.gz", REASON_SIBLING, job_id + 1),
    ]
    try:
        for _ in range(2):
            assert run_stage(engine, refs, tmp_path, workers=1) == 2
        with engine.connect() as conn:
            n = conn.execute(
                text("select count(*) from testing.test_results where job_id = :j"), {"j": job_id}
            ).scalar_one()
            err = conn.execute(
                text("select error from testing.log_parses where job_id = :j"), {"j": job_id + 1}
            ).scalar_one()
        assert n == 2
        assert err == "missing_file"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "delete from testing.test_results where job_id between 987000001 and 987000002"
                )
            )
            conn.execute(
                text("delete from testing.log_parses where job_id between 987000001 and 987000002")
            )
            conn.execute(text("delete from testing.test_ids where repo = 'o/r'"))
