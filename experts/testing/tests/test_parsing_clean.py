"""Cleaning a raw GitHub Actions log: prefixes, colours, steps, size caps, damaged archives."""

from __future__ import annotations

import dataclasses
import gzip
from pathlib import Path

from testing_expert.parsing import (
    ParsingConfig,
    clean_bytes,
    clean_file,
    clean_line,
    clean_text,
    load_config,
)

TS = "2026-08-10T16:12:20.9525588Z"


def _small(**overrides: int) -> ParsingConfig:
    cfg = load_config()
    return dataclasses.replace(cfg, clean=dataclasses.replace(cfg.clean, **overrides))


def test_config_file_loads_and_is_sane() -> None:
    cfg = load_config()
    assert cfg.clean.head_bytes < cfg.clean.max_input_bytes
    assert cfg.clean.max_line_bytes > 1000
    assert len(cfg.normalise.rules) >= 8
    assert cfg.fixtures.max_bytes == 65536


def test_clean_line_strips_timestamp_ansi_and_bom() -> None:
    assert clean_line(f"\ufeff{TS} Current runner version: '2.336.0'\n") == (
        "Current runner version: '2.336.0'"
    )
    assert clean_line(f"{TS} \x1b[32m✓\x1b[39m works \x1b[2m(5 ms)\x1b[22m") == "✓ works (5 ms)"
    assert clean_line(f"{TS}") == ""
    assert clean_line("no timestamp here") == "no timestamp here"
    assert clean_line(f"{TS}   indented") == "  indented"


def test_clean_line_keeps_the_last_carriage_return_redraw() -> None:
    assert clean_line(f"{TS} progress 10%\rprogress 50%\rprogress 100%\r\n") == "progress 100%"
    assert clean_line("\r") == ""


def test_clean_line_drops_control_characters_but_keeps_tabs() -> None:
    assert clean_line("a\x00b\x07c\x1bd\te") == "abcd\te"
    assert clean_line("\x1b]0;window title\x07text") == "text"


def _log(*lines: str) -> str:
    return "\n".join(f"{TS} {line}" if line else TS for line in lines) + "\n"


def test_steps_split_on_run_groups() -> None:
    log = _log(
        "Current runner version: '2.336.0'",
        "##[group]Run actions/checkout@v4",
        "with:",
        "  fetch-depth: 1",
        "##[endgroup]",
        "Syncing repository",
        "##[group]Run yarn jest --ci",
        "env:",
        "  CI: true",
        "##[endgroup]",
        "\x1b[1mPASS\x1b[22m src/a.test.js",
        "##[error]Process completed with exit code 1.",
    )
    cleaned = clean_text(log)
    assert [s.command for s in cleaned.steps] == ["", "actions/checkout@v4", "yarn jest --ci"]
    assert cleaned.steps[1].script == ("with:", "  fetch-depth: 1")
    assert cleaned.steps[2].script == ("env:", "  CI: true")
    assert cleaned.step_lines(cleaned.steps[0]) == ("Current runner version: '2.336.0'",)
    assert cleaned.step_lines(cleaned.steps[1]) == ("Syncing repository",)
    assert cleaned.step_lines(cleaned.steps[2]) == (
        "PASS src/a.test.js",
        "Process completed with exit code 1.",
    )
    assert "##[" not in cleaned.text()
    assert not cleaned.truncated


def test_runner_bookkeeping_markers_are_dropped() -> None:
    cleaned = clean_text(
        _log(
            "##[start-action display=Run x;id=__self.__run]",
            "kept",
            "##[end-action id=__self.__run;outcome=failure;conclusion=failure;duration_ms=12]",
            "##[add-matcher]/home/runner/matcher.json",
            "##[warning]Node 20 is deprecated",
            "##[notice]hello",
        )
    )
    assert cleaned.lines == ("kept", "Node 20 is deprecated", "hello")


def test_other_groups_keep_their_title_as_a_line() -> None:
    cleaned = clean_text(_log("##[group]Test output", "a", "##[endgroup]", "b"))
    assert cleaned.lines == ("Test output", "a", "b")
    assert len(cleaned.steps) == 1


def test_group_that_never_closes_does_not_swallow_the_log() -> None:
    cfg = _small(max_script_lines=3)
    cleaned = clean_text(_log("##[group]Run npm test", "a", "b", "c", "d", "e"), cfg)
    assert cleaned.steps[0].command == "npm test"
    assert cleaned.lines == ("d", "e")


def test_a_cut_marker_ends_a_group_whose_endgroup_was_cut_away() -> None:
    """A trimmed fixture can lose a group's `##[endgroup]`; the marker line ends the header."""
    cleaned = clean_text(
        _log("##[group]Run yarn test", "env:", "[fixture trimmed: 9 lines omitted]", "  3 passing")
    )
    assert cleaned.steps[0].command == "yarn test"
    assert cleaned.steps[0].script == ("env:",)
    assert cleaned.lines == ("[fixture trimmed: 9 lines omitted]", "  3 passing")


def test_empty_and_blank_input() -> None:
    assert clean_text("").lines == ()
    assert clean_text("").steps == ()
    assert clean_bytes(b"\n\n").lines == ("", "")


def test_long_lines_are_cut() -> None:
    cfg = _small(max_line_bytes=50)
    cleaned = clean_text("x" * 500 + "\nshort\n", cfg)
    assert cleaned.lines == ("x" * 50, "short")
    assert cleaned.bytes_read == 507


def test_long_line_without_newline_at_the_end() -> None:
    cfg = _small(max_line_bytes=50)
    assert clean_text("a\n" + "y" * 200, cfg).lines == ("a", "y" * 50)


def test_over_cap_keeps_head_and_tail_with_a_marker() -> None:
    cfg = _small(max_input_bytes=400, head_bytes=100)
    text = "".join(f"line {i:04d} padding padding\n" for i in range(100))
    cleaned = clean_text(text, cfg)
    assert cleaned.truncated
    assert cleaned.lines_dropped > 0
    assert cleaned.lines[0] == "line 0000 padding padding"
    assert cleaned.lines[-1] == "line 0099 padding padding"
    marker = [x for x in cleaned.lines if x.startswith("[log truncated:")]
    assert len(marker) == 1
    assert any("kept head and tail" in w for w in cleaned.warnings)
    assert sum(len(x) + 1 for x in cleaned.lines) < 700


def test_scan_limit_stops_reading() -> None:
    cfg = _small(max_scan_bytes=100)
    cleaned = clean_text("0123456789\n" * 100, cfg)
    assert cleaned.truncated
    assert len(cleaned.lines) <= 10
    assert any("stopped reading" in w for w in cleaned.warnings)


def test_gzip_file_plain_file_and_bytes_agree(tmp_path: Path) -> None:
    text = _log("##[group]Run npm test", "##[endgroup]", "PASS a.test.js", "Tests: 1 passed")
    gz = tmp_path / "1.log.gz"
    gz.write_bytes(gzip.compress(text.encode()))
    plain = tmp_path / "1.log"
    plain.write_text(text)
    a = clean_file(gz)
    assert a.lines == clean_file(plain).lines == clean_bytes(gzip.compress(text.encode())).lines
    assert a.lines == ("PASS a.test.js", "Tests: 1 passed")


def test_damaged_gzip_gives_a_shorter_log_not_an_error(tmp_path: Path) -> None:
    text = "".join(f"{TS} line {i}\n" for i in range(5000))
    blob = gzip.compress(text.encode())
    bad = tmp_path / "bad.log.gz"
    bad.write_bytes(blob[: len(blob) // 2])
    cleaned = clean_file(bad)
    assert cleaned.truncated
    assert any("ended early" in w for w in cleaned.warnings)
    assert 0 < len(cleaned.lines) < 5000
    assert clean_bytes(b"\x1f\x8b" + b"not really gzip").truncated


def test_invalid_utf8_is_replaced_not_raised() -> None:
    cleaned = clean_bytes(b"ok \xff\xfe bad\nnext\n")
    assert cleaned.lines[1] == "next"
    assert cleaned.lines[0].startswith("ok ")
