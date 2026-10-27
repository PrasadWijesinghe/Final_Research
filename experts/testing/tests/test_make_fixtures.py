"""The fixture tool's pure parts: trimming, scrubbing, manifest and stub handling (no database)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from testing_expert.parsing import load_config
from testing_expert.parsing.make_fixtures import (
    EMAIL_RE,
    FixtureEntry,
    JobMeta,
    TrimReport,
    build_fixture_text,
    fixture_name,
    load_manifest,
    main,
    merge_expected,
    stub_expected,
    trim_lines,
)

TS = "2026-08-10T16:12:20.9525588Z"
SETTINGS = load_config().fixtures


def _jest_log(tests: int = 400) -> list[str]:
    lines = [f"{TS} Current runner version: '2.336.0'"]
    lines += [f"{TS} ##[group]Run actions/checkout@v4", f"{TS} ##[endgroup]"]
    lines += [f"{TS} setup line {i}" for i in range(60)]
    lines += [f"{TS} ##[group]Run yarn jest", f"{TS} env:", f"{TS} ##[endgroup]"]
    lines += [f"{TS} PASS src/test{i}.test.js (5 ms)" for i in range(tests)]
    lines += [f"{TS} \x1b[1mFAIL\x1b[22m src/broken.test.js", f"{TS}   ● broken › works"]
    lines += [f"{TS}     TypeError: x is not a function"]
    lines += [f"{TS}       at f (src/broken.js:3:9)", f"{TS} Test Suites: 1 failed, 400 passed"]
    lines += [f"{TS} Tests:       1 failed, 400 passed, 401 total"]
    lines += [f"{TS} ##[error]Process completed with exit code 1.", f"{TS} Post job cleanup."]
    return lines


def test_a_small_log_is_returned_unchanged() -> None:
    lines = _jest_log(5)
    assert trim_lines(lines, SETTINGS) == lines


def test_long_lines_are_dropped_and_counted() -> None:
    report = TrimReport()
    lines = ["short", "x" * (SETTINGS.max_line_bytes + 1), "also short"]
    assert trim_lines(lines, SETTINGS, report) == ["short", "also short"]
    assert report.long_lines_dropped == 1


def test_trimming_keeps_the_summary_failure_and_last_lines_within_the_cap() -> None:
    lines = _jest_log(400)
    cap = 6000
    report = TrimReport()
    kept = trim_lines(lines, dataclasses.replace(SETTINGS, max_bytes=cap), report)
    text = "\n".join(kept)
    assert len(text.encode()) <= cap
    assert "Tests:       1 failed, 400 passed, 401 total" in text
    assert "● broken › works" in text
    assert "Process completed with exit code 1." in text
    assert "Run yarn jest" in text  # the test step's command stays
    assert report.lines_omitted > 0
    markers = [x for x in kept if "[fixture trimmed:" in x]
    assert markers and all(x.startswith(TS) for x in markers)  # markers keep the timestamp shape
    assert len(kept) < len(lines)


def test_trim_marker_counts_add_up() -> None:
    lines = _jest_log(400)
    kept = trim_lines(lines, dataclasses.replace(SETTINGS, max_bytes=6000))
    omitted = sum(
        int(x.split("trimmed: ")[1].split(" lines")[0]) for x in kept if "[fixture trimmed:" in x
    )
    real = [x for x in kept if "[fixture trimmed:" not in x]
    assert len(real) + omitted == len(lines)


def test_log_without_a_test_step_keeps_the_failing_step() -> None:
    lines = [f"{TS} ##[group]Run npm ci", f"{TS} ##[endgroup]"]
    lines += [f"{TS} install noise {i}" for i in range(300)]
    lines += [f"{TS} ##[group]Run npm run build", f"{TS} ##[endgroup]"]
    lines += [
        f"{TS} error TS2307: Cannot find module 'tsx'",
        f"{TS} Process completed with exit code 1.",
    ]
    lines += [f"{TS} ##[group]Run post step", f"{TS} ##[endgroup]"]
    lines += [f"{TS} cleanup {i}" for i in range(50)]
    kept = trim_lines(lines, dataclasses.replace(SETTINGS, max_bytes=4500))
    assert any("error TS2307" in x for x in kept)


def test_scrub_maps_emails_and_keeps_everything_else() -> None:
    text = (
        "contact maintainer@example.invalid or Ada.Lovelace+ci@example.co.uk\n"
        "bot 41898282+github-actions[bot]@users.noreply.github.com\n"
        "pkg@1.2.3-rc.next and glob@7.2.3 and @babel/core@7.0.0 stay\n"
    )
    out = build_fixture_text(text.encode(), SETTINGS)
    assert out.count("user@example.invalid") == 3
    assert "pkg@1.2.3-rc.next and glob@7.2.3 and @babel/core@7.0.0 stay" in out
    assert [m for m in EMAIL_RE.findall(out) if m != "user@example.invalid"] == []


def test_scrub_runs_the_sanitiser() -> None:
    token = "ghp_" + "a1B2c3D4e5" * 4
    out = build_fixture_text(f"{TS} clone https://x:{token}@github.com/o/r\n".encode(), SETTINGS)
    assert token not in out
    assert "[REDACTED:" in out


def test_build_keeps_timestamps_colours_and_final_newline() -> None:
    raw = f"{TS} \x1b[32mok\x1b[39m\n".encode()
    assert build_fixture_text(raw, SETTINGS) == f"{TS} \x1b[32mok\x1b[39m\n"
    assert build_fixture_text(b"", SETTINGS) == ""


def test_fixture_name() -> None:
    assert fixture_name("jestjs/jest", 123) == "jestjs__jest__123"


def test_stub_and_merge_keep_hand_written_expectations() -> None:
    entry = FixtureEntry(7, "jest", "o/r", "why")
    stub = stub_expected(entry, JobMeta(7, "o/r", "failure", "Linux", "logs/x.gz"))
    assert stub == {"framework": "jest", "repo": "o/r", "job_id": 7, "conclusion": "failure"}
    existing = {"framework": "old", "failures": ["a"], "summary": {"failed": 1}}
    merged = merge_expected(existing, stub)
    assert merged["framework"] == "jest"
    assert merged["failures"] == ["a"]
    assert merged["summary"] == {"failed": 1}
    assert merge_expected(None, stub) == stub


def test_committed_manifest_loads() -> None:
    manifest = load_manifest(Path(__file__).parent / "fixtures" / "logs" / "manifest.yaml")
    assert len(manifest.fixtures) >= 42
    assert manifest.categories  # --suggest has something to search for
    assert {e.framework for e in manifest.fixtures} == {"jest", "mocha", "vitest", "other"}


def test_manifest_rejects_duplicates_and_unknown_frameworks(tmp_path: Path) -> None:
    dup = tmp_path / "dup.yaml"
    dup.write_text(
        "fixtures:\n  jest:\n    - {job_id: 1, repo: a/b}\n    - {job_id: 1, repo: a/b}\n"
    )
    with pytest.raises(ValueError, match="twice"):
        load_manifest(dup)
    bad = tmp_path / "bad.yaml"
    bad.write_text("fixtures:\n  qunit:\n    - {job_id: 1, repo: a/b}\n")
    with pytest.raises(ValueError, match="unknown framework"):
        load_manifest(bad)


def test_cli_refuses_to_run_without_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main([]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().err
