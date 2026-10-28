"""`parse_log` and the detector: inputs, confidence, merging, and that it never raises."""

from __future__ import annotations

import gzip
import random
import time
from pathlib import Path

import pytest
from testing_expert.parsing import (
    CleanedLog,
    clean_text,
    detect,
    jest,
    mocha,
    parse_log,
    vitest,
)
from testing_expert.parsing import parse as parse_module

LOGS = Path(__file__).parent / "fixtures" / "logs"
FIXTURES = sorted(LOGS.glob("*/*.log"))
TS = "2026-08-10T16:12:20.9525588Z"
SEED = 42  # DEFAULT: fixed seed so a failure is reproducible

MOCHA_TEXT = (
    "  1 passing (1s)\n  1 failing\n\n  1) s\n       t:\n     Error: x\n"
    "      at f (test/a.test.js:1:1)\n"
)


# ---------------------------------------------------------------- detector


def test_a_summary_line_scores_its_framework() -> None:
    assert detect(["Test Suites: 1 passed, 1 total"])[0].framework == "jest"
    assert detect(["  12 passing (1s)"])[0].framework == "mocha"
    assert detect([" Test Files  1 passed (1)"])[0].framework == "vitest"
    assert detect(["ℹ tests 30"])[0].framework == "other"
    assert detect(["TAP version 13"])[0].framework == "other"
    assert detect(["nothing to see"]) == []


def test_a_lone_pass_line_is_too_weak_to_run_a_parser() -> None:
    (only,) = detect(["PASS src/a.test.js"])
    assert only.framework == "jest" and only.score < 3
    assert parse_log("PASS src/a.test.js\n").framework == "other"


def test_the_step_command_breaks_a_tie() -> None:
    lines = ["Test Suites: 1 passed, 1 total", "  1 passing (1s)"]
    assert detect(lines, ["yarn mocha test"])[0].framework == "mocha"
    assert detect(lines, ["npx jest --ci"])[0].framework == "jest"


def test_the_repo_hint_is_the_last_tie_break() -> None:
    lines = ["Test Suites: 1 passed, 1 total", "  1 passing (1s)"]
    assert detect(lines, [], hint="mocha")[0].framework == "mocha"
    assert detect(lines, ["yarn jest"], hint="mocha")[0].framework == "jest"


def test_a_wrong_hint_does_not_override_the_log() -> None:
    result = parse_log(MOCHA_TEXT, hint="vitest")
    assert result.framework == "mocha"


def test_node_test_and_tap_output_is_other_and_not_parsed() -> None:
    result = parse_log("TAP version 13\nnot ok 1 - broken\n# tests 1\n# fail 1\n")
    assert result.framework == "other" and result.failures == () and result.summary is None
    assert result.confidence == 0.0 and result.parsed_ok


def test_a_tstyche_summary_is_not_mistaken_for_jest() -> None:
    text = (
        "Targets:    1 failed, 1 total\nTest files: 1 failed, 1 total\n"
        "Tests:      1 failed, 21 passed, 22 total\n"
    )
    assert parse_log(text).framework == "other"


def test_two_runners_in_one_log_are_both_parsed_and_merged() -> None:
    text = (
        "FAIL a.test.js\n  ● a › one\n\n    boom\n\n"
        "Test Suites: 1 failed, 1 total\nTests:       1 failed, 1 total\n" + MOCHA_TEXT
    )
    result = parse_log(text)
    assert {f.full_name for f in result.failures} == {"a > one", "s > t"}
    assert result.summary is not None and result.summary.failed == 2
    assert any("several test runners" in w for w in result.warnings)
    assert result.confidence == 1.0


# ---------------------------------------------------------------- inputs


def test_parse_log_accepts_text_bytes_gzip_a_path_and_a_cleaned_log(tmp_path: Path) -> None:
    raw = "\n".join(f"{TS} {line}" for line in MOCHA_TEXT.split("\n")) + "\n"
    results = [
        parse_log(MOCHA_TEXT),
        parse_log(raw),
        parse_log(raw.encode()),
        parse_log(gzip.compress(raw.encode())),
        parse_log(clean_text(raw)),
    ]
    plain = tmp_path / "job.log"
    plain.write_text(raw)
    packed = tmp_path / "job.log.gz"
    packed.write_bytes(gzip.compress(raw.encode()))
    results += [parse_log(plain), parse_log(packed)]
    for result in results:
        assert result.framework == "mocha"
        assert [f.full_name for f in result.failures] == ["s > t"]
        assert result.confidence == 1.0


def test_timestamps_ansi_and_group_markers_do_not_matter() -> None:
    raw = (
        f"{TS} ##[group]Run yarn mocha\n{TS} env:\n{TS} ##[endgroup]\n"
        f"{TS} \x1b[92m  1 passing\x1b[0m\x1b[90m (1s)\x1b[0m\n"
        f"{TS} \x1b[31m  1 failing\x1b[0m\n{TS} \n"
        f"{TS}   1) suite\n{TS}        case:\n{TS}      Error: boom\n"
        f"{TS}       at f (test/a.test.js:1:1)\n"
        f"{TS} ##[error]Process completed with exit code 1.\n"
    )
    result = parse_log(raw)
    assert [f.full_name for f in result.failures] == ["suite > case"]
    assert result.confidence == 1.0


def test_an_unclosed_run_group_in_a_trimmed_log_does_not_hide_the_output() -> None:
    """A fixture trimmed inside a group loses its endgroup: the trimmed marker ends the header."""
    raw = (
        f"{TS} ##[group]Run yarn mocha\n{TS} env:\n{TS} [fixture trimmed: 50 lines omitted]\n"
        + "".join(f"{TS} {line}\n" for line in MOCHA_TEXT.split("\n"))
    )
    assert parse_log(raw).failures[0].full_name == "s > t"


# ---------------------------------------------------------------- confidence and ok flag


def test_confidence_is_one_for_a_clean_pass_and_zero_for_no_test_output() -> None:
    passing = parse_log("Test Suites: 1 passed, 1 total\nTests:       3 passed, 3 total\n")
    assert (passing.confidence, passing.parsed_ok, passing.failures) == (1.0, True, ())
    nothing = parse_log("npm ERR! code ELIFECYCLE\n")
    assert (nothing.framework, nothing.confidence, nothing.parsed_ok) == ("other", 0.0, True)


def test_a_failure_count_that_disagrees_with_the_summary_gets_a_warning_and_0_7() -> None:
    result = parse_log("  1 passing\n  3 failing\n\n  1) s\n       t:\n     Error: x\n")
    assert result.confidence == 0.7 and result.parsed_ok is True
    assert any("1 failed test(s) found" in w and "report 3" in w for w in result.warnings)


def test_failures_after_the_last_summary_disagree() -> None:
    text = (
        "Test Suites: 1 passed, 1 total\nTests:       1 passed, 1 total\n"
        "FAIL a.test.js\n  ● late failure\n\n    boom\n"
    )
    result = parse_log(text)
    assert [f.full_name for f in result.failures] == ["late failure"]
    assert result.confidence == 0.7


def test_the_truncated_flag_and_cleaner_warnings_are_passed_on() -> None:
    cleaned = clean_text("\n".join([MOCHA_TEXT] * 3))
    result = parse_log(cleaned)
    assert result.truncated == cleaned.truncated
    assert result.summary_count == 3


def test_summary_count_and_summing_for_a_multi_summary_log() -> None:
    result = parse_log("  2 passing\n  1 pending\n\n  3 passing\n")
    assert result.summary_count == 2
    assert result.summary is not None
    assert (result.summary.passed, result.summary.skipped, result.summary.total) == (5, 1, 6)
    assert any("2 summaries found" in w for w in result.warnings)


# ---------------------------------------------------------------- never raises


def test_a_parser_that_raises_is_reported_without_the_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(lines: object, config: object = None) -> None:
        raise ValueError("secret-token-abc")

    monkeypatch.setitem(parse_module._PARSERS, "mocha", broken)
    result = parse_log(MOCHA_TEXT)
    assert result.parsed_ok is False
    assert any("mocha parser failed: ValueError" in w for w in result.warnings)
    assert "secret-token-abc" not in " ".join(result.warnings)


def test_any_exception_in_the_pipeline_gives_parsed_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret-token-xyz")

    monkeypatch.setattr(parse_module, "clean_text", broken)
    result = parse_log("anything")
    assert (result.framework, result.parsed_ok, result.confidence) == ("other", False, 0.0)
    assert result.failures == () and result.summary is None
    assert result.warnings == ("log could not be parsed: RuntimeError",)


def test_a_missing_file_does_not_raise(tmp_path: Path) -> None:
    result = parse_log(tmp_path / "nope.log")
    assert result.parsed_ok is False and result.warnings


@pytest.mark.parametrize("log", FIXTURES, ids=lambda p: p.stem)
def test_every_fixture_cut_at_several_points_never_raises(log: Path) -> None:
    data = log.read_bytes()
    cuts = sorted(
        {
            0,
            1,
            17,
            len(data) // 7,
            len(data) // 3,
            len(data) // 2,
            len(data) * 4 // 5,
            len(data) - 1,
        }
    )
    for cut in cuts:
        part = data[:cut]
        result = parse_log(part)  # byte cuts can split a UTF-8 character or an ANSI escape
        assert 0.0 <= result.confidence <= 1.0
        lines = clean_text(part.decode("utf-8", errors="replace")).lines
        for parser in (jest.parse, mocha.parse, vitest.parse):
            parser(lines)  # the parsers themselves must not raise either


@pytest.mark.parametrize("log", FIXTURES[::5], ids=lambda p: p.stem)
def test_every_fixture_cut_at_line_boundaries_never_raises(log: Path) -> None:
    lines = log.read_text(encoding="utf-8").split("\n")
    for cut in range(1, len(lines), max(len(lines) // 9, 1)):
        parse_log("\n".join(lines[:cut]))


FRAGMENTS = [
    "FAIL ", "PASS ", " FAIL  ", "  ● ", " › ", " > ", "Test Suites: ", "Tests:       ",
    "1 failed", "2 passed", "  1) ", "  12 passing", "  3 failing", "(1s)", "✔ ", "✓ ", "✕ ",
    "× ", "- ", " ❯ ", "⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯", "⎯⎯⎯⎯⎯⎯⎯[1/2]⎯",
    " Test Files  1 failed (2)", "      Tests  1 failed (3)", "Summary of all failing tests",
    "at fn (src/a.js:1:2)", "TypeError: x", "AssertionError [ERR_X]: y", "Error:", "(retry x2)",
    "[1/1]", 'Ran all test suites."', "[fixture trimmed: 5 lines omitted]", "\t", "  ", "    ",
    "      ", "\x00", "é", "·",
]  # fmt: skip


def _random_log(rng: random.Random) -> str:
    lines = []
    for _ in range(rng.randint(0, 60)):
        lines.append("".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(0, 6))))
    return "\n".join(lines)


def test_seeded_random_logs_never_raise_and_stay_valid() -> None:
    rng = random.Random(SEED)
    for _ in range(600):
        text = _random_log(rng)
        result = parse_log(text)
        assert result.parsed_ok or result.warnings
        assert 0.0 <= result.confidence <= 1.0
        lines = text.split("\n")
        for parser in (jest.parse, mocha.parse, vitest.parse):
            parser(lines)


def test_random_bytes_never_raise() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(200):
        parse_log(rng.randbytes(rng.randint(0, 4096)))


def test_a_huge_summary_free_log_parses_fast() -> None:
    """150k spec lines (about 6 MB) parse in a few seconds: every pass over the lines is linear."""
    lines = [f"{TS}     ✔ test number {i} ({i % 50}ms)" for i in range(150_000)]
    lines.append(f"{TS}   150000 passing (4m)")
    started = time.perf_counter()
    result = parse_log("\n".join(lines))
    assert time.perf_counter() - started < 20
    assert result.summary is not None and result.summary.passed == 150_000
    assert len(result.results) <= 50_000  # DEFAULT parse.max_results caps the list
    assert any("results" in w for w in result.warnings)


def test_failures_are_capped_and_the_result_is_marked_truncated() -> None:
    block = "  {n}) s\n       t{n}:\n     Error: x\n      at f (test/a.test.js:1:1)\n\n"
    text = "  1500 failing\n\n" + "".join(block.format(n=n) for n in range(1, 1501))
    result = parse_log(text)
    assert len(result.failures) == 1000  # DEFAULT parse.max_failures
    assert result.truncated is True
    assert any("stopped after 1000 failure blocks" in w for w in result.warnings)
    assert result.confidence == 0.7  # 1000 built, the summary says 1500


def test_a_missing_config_is_reported_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TEST_LOG_PARSING_FILE", str(tmp_path / "nope.yaml"))
    result = parse_log(MOCHA_TEXT)
    assert (result.framework, result.parsed_ok, result.confidence) == ("other", False, 0.0)
    (warning,) = result.warnings
    assert "parsing config not found" in warning and "TEST_LOG_PARSING_FILE" in warning


def test_an_unusable_config_is_reported_by_error_type_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("clean: {}\n")
    monkeypatch.setenv("TEST_LOG_PARSING_FILE", str(bad))
    result = parse_log(MOCHA_TEXT)
    assert result.parsed_ok is False
    assert result.warnings == ("parsing config is not usable: ValueError",)


def test_cleaned_log_input_is_not_mutated() -> None:
    cleaned = clean_text(MOCHA_TEXT)
    before = (cleaned.lines, cleaned.steps)
    parse_log(cleaned)
    assert (cleaned.lines, cleaned.steps) == before
    assert isinstance(cleaned, CleanedLog)
