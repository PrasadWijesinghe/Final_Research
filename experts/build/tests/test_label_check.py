"""The manual label check helper (build-label-check): blind sheet, key, and scores."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from build_expert.dataset.builder import JOB_SCHEMA
from build_expert.labels.check import KEY_COLUMNS, SHEET_COLUMNS, main, sample, score


def _decision(
    i: int, claimed: bool, rule: str, sub: str | None, attempt: int = 1
) -> dict[str, Any]:
    return {
        "repo": "acme/web",
        "run_id": 100 + i,
        "attempt": attempt,
        "job_id": 1000 + i,
        "job_name": f"job {i}",
        "failed_step": "Run pnpm run typecheck" if claimed else "Run pnpm test",
        "log_status": "ok",
        "claimed": claimed,
        "rule_name": rule,
        "sub_label": sub,
        "confidence": "high" if claimed else None,
        "notes": None,
        "error_excerpt": "src/a.ts:1 TS2322 Type 'string' is not assignable" if claimed else None,
        "url": f"https://github.com/acme/web/actions/runs/{100 + i}/job/{1000 + i}",
    }


def _decisions() -> list[dict[str, Any]]:
    rows = [_decision(i, True, "build_step_tool_error", "tsc") for i in range(10)]
    rows += [_decision(i, True, "build_step_name_only", "build_other") for i in range(10, 13)]
    rows += [_decision(i, False, "not_build_test_failure", None) for i in range(13, 30)]
    rows += [_decision(i, False, "not_build_setup_step", None) for i in range(30, 35)]
    rows.append(_decision(99, True, "build_step_tool_error", "tsc", attempt=2))
    return rows


def test_sample_is_stratified_first_attempt_only_and_reproducible() -> None:
    picked = sample(_decisions(), per_rule=4, not_build=6, seed=3)
    rules = [p["rule_name"] for p in picked]
    assert rules.count("build_step_tool_error") == 4
    assert rules.count("build_step_name_only") == 3  # only 3 exist
    assert rules.count("not_build_test_failure") == 3 and rules.count("not_build_setup_step") == 3
    assert all(p["attempt"] == 1 for p in picked)
    assert [p["job_id"] for p in picked] == [
        p["job_id"] for p in sample(_decisions(), per_rule=4, not_build=6, seed=3)
    ]


def _fill(sheet: Path, out: Path, answers: dict[str, tuple[str, str]]) -> None:
    with sheet.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["is_build_failure"], row["kind"] = answers.get(row["id"], ("", ""))
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def test_cli_sample_then_score(tmp_path: Path) -> None:
    folder = tmp_path / "v1"
    folder.mkdir()
    pq.write_table(
        pa.Table.from_pylist(_decisions(), schema=JOB_SCHEMA), folder / "job_decisions.parquet"
    )
    assert main(["sample", "--dataset", str(folder), "--per-rule", "4", "--not-build", "6"]) == 0
    sheet, key = folder / "label_check_sheet.csv", folder / "label_check_key.csv"
    with sheet.open(newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    assert tuple(header) == SHEET_COLUMNS
    assert "claimed" not in header and "rule_name" not in header  # the sheet is blind
    with key.open(newline="", encoding="utf-8-sig") as fh:
        key_rows = list(csv.DictReader(fh))
    assert tuple(key_rows[0]) == KEY_COLUMNS

    # Person A agrees with the rules except on one claimed job; person B agrees fully.
    truth = {r["id"]: (r["claimed"], r["sub_label"]) for r in key_rows}
    first_claimed = next(i for i, (c, _) in truth.items() if c == "yes")
    a = {i: (c, s) for i, (c, s) in truth.items()}
    a[first_claimed] = ("no", "")
    _fill(sheet, tmp_path / "A.csv", a)
    _fill(sheet, tmp_path / "B.csv", truth)
    text = score(key, [tmp_path / "A.csv", tmp_path / "B.csv"])
    assert "Rules against `A` (13 jobs answered)" in text
    assert "Precision of the rules' build claims: 85.7%" in text
    assert "Rules against `B`" in text and "Cohen's kappa: 1.0" in text
    assert "`A` against `B` (13 jobs both answered)" in text


def test_score_with_no_answers(tmp_path: Path) -> None:
    key = tmp_path / "key.csv"
    key.write_text("id,job_id,claimed,rule_name,sub_label,confidence\n1,5,yes,r,tsc,high\n")
    sheet = tmp_path / "S.csv"
    sheet.write_text(",".join(SHEET_COLUMNS) + "\n1,acme/web,u,j,s,e,,,\n")
    assert "no answers yet" in score(key, [sheet])
