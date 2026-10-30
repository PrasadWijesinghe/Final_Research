"""Node's built-in test runner: TAP and spec output, detection, and what triage reads from it.

The Windows TAP fixture is a trimmed real log (a failing `node --test` run of the demo repo); the
other fixtures in `fixtures/node_test/` follow Node's documented output for the shapes the real
log does not have (nested suites, timeouts, the spec reporter).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from testing_expert.parsing import clean_file, detect, nodetest, parse_log
from testing_expert.triage import evidence as tev
from testing_expert.triage.features import (
    FEATURE_NAMES,
    FRAMEWORKS,
    TriageEvidence,
    feature_row,
    read_log,
)

FIXTURES = Path(__file__).parent / "fixtures" / "node_test"
LOGS = Path(__file__).parent / "fixtures" / "logs"


def _lines(name: str) -> list[str]:
    return list(clean_file(FIXTURES / name).lines)


# ---------------------------------------------------------------- TAP reporter


def test_tap_failure_on_windows_gives_message_file_and_summary() -> None:
    result = parse_log(FIXTURES / "tap_fail_windows.log")
    assert result.framework == "node-test"
    assert (result.parsed_ok, result.confidence, result.granularity) == (True, 1.0, "test")
    s = result.summary
    assert s is not None
    assert (s.passed, s.failed, s.total, s.skipped) == (4, 1, 5, 0)
    (f,) = result.failures
    assert f.full_name == "order is saved before the timer fires"
    assert f.file == "test/flaky-order.test.js"  # D:\a\piq-demo\piq-demo\ removed, `\\` undoubled
    assert f.file_source == "header"
    assert f.error_type == "AssertionError"
    assert f.message == (
        "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n\n1 !== 0"
    )
    assert f.diff == "expected: 0\nactual: 1"
    assert f.frames[0].path == "test/flaky-order.test.js"  # file:///D:/a/... URL mapped too
    assert f.frames[0].kind == "project"
    assert [r.outcome for r in result.results] == ["passed"] * 4 + ["failed"]


def test_tap_pass_has_results_a_skip_and_no_failures() -> None:
    result = parse_log(FIXTURES / "tap_pass.log")
    assert result.framework == "node-test" and result.failures == ()
    assert [(r.full_name, r.outcome) for r in result.results] == [
        ("adds", "passed"),
        ("subtracts", "passed"),
        ("later", "skipped"),
    ]
    assert (result.confidence, result.summary_count) == (1.0, 1)


def test_nested_subtests_are_joined_and_the_failing_parent_is_not_counted() -> None:
    result = parse_log(FIXTURES / "tap_nested.log")
    names = [f.full_name for f in result.failures]
    assert "cart > discounts" in names  # describe name + test name
    assert "cart" not in names  # failureType 'subtestsFailed' only repeats its children
    discounts = next(f for f in result.failures if f.full_name == "cart > discounts")
    assert discounts.file == "test/cart.test.js"  # POSIX /home/runner/work/demo/demo/ removed
    assert discounts.error_type == "AssertionError"
    assert discounts.frames[0].line == 16


def test_a_timeout_is_a_failure_and_a_test_cancelled_by_its_parent_is_not() -> None:
    result = parse_log(FIXTURES / "tap_nested.log")
    by_name = {f.full_name: f for f in result.failures}
    assert set(by_name) == {"cart > discounts", "cart > slow checkout"}
    assert by_name["cart > slow checkout"].message == "test timed out after 2000ms"
    # Node counted the timeout and the cancelled test under `cancelled`; the run still agrees.
    assert result.summary is not None and result.summary.failed == 3
    assert (result.parsed_ok, result.confidence) == (True, 1.0)
    assert "cart > never reached" in {r.full_name for r in result.results}


def test_cancelled_tests_are_kept_when_nothing_else_failed() -> None:
    lines = [
        "TAP version 13",
        "# Subtest: only",
        "not ok 1 - only",
        "  ---",
        "  duration_ms: 0",
        "  type: 'test'",
        "  failureType: 'cancelledByParent'",
        "  error: 'test did not finish before its parent and was cancelled'",
        "  ...",
        "1..1",
        "# tests 1",
        "# suites 0",
        "# pass 0",
        "# fail 0",
        "# cancelled 1",
    ]
    (f,) = nodetest.parse(lines).failures
    assert f.full_name == "only"


def test_a_skip_and_a_todo_directive_are_not_failures() -> None:
    lines = [
        "# Subtest: a",
        "not ok 1 - a # TODO fix later",
        "  ---",
        "  failureType: 'testCodeFailure'",
        "  error: 'boom'",
        "  ...",
        "ok 2 - b # SKIP",
        "# tests 2",
        "# pass 0",
        "# fail 0",
        "# todo 1",
        "# skipped 1",
    ]
    part = nodetest.parse(lines)
    assert part.failures == ()
    assert [r.outcome for r in part.results] == ["todo", "skipped"]


def test_an_unclosed_yaml_block_does_not_swallow_the_next_test() -> None:
    lines = [
        "not ok 1 - first",
        "  ---",
        "  failureType: 'testCodeFailure'",
        "  error: 'one'",
        "not ok 2 - second",
        "  ---",
        "  failureType: 'testCodeFailure'",
        "  error: 'two'",
        "  ...",
    ]
    names = [f.full_name for f in nodetest.parse(lines).failures]
    assert "second" in names


def test_a_log_cut_before_the_summary_still_yields_the_failure() -> None:
    lines = _lines("tap_fail_windows.log")
    cut = [x for x in lines if not x.startswith("# ") or x.startswith("# Subtest")]
    result = parse_log("\n".join(cut))
    assert result.framework == "node-test"
    assert [f.full_name for f in result.failures] == ["order is saved before the timer fires"]
    assert result.confidence == 0.4  # failures but no summary


# ---------------------------------------------------------------- spec reporter


def test_spec_failure_gets_nested_name_file_and_error_from_the_failing_section() -> None:
    result = parse_log(FIXTURES / "spec_fail.log")
    assert result.framework == "node-test"
    (f,) = result.failures  # the failing suite `cart` is not a second failure
    assert f.full_name == "cart > discounts"
    assert f.file == "test/cart.test.js"
    assert f.error_type == "AssertionError"
    assert "90 !== 95" in f.message
    assert (result.confidence, result.summary_count) == (1.0, 1)
    by_name = {r.full_name: r.outcome for r in result.results}
    assert by_name == {
        "cart > totals": "passed",
        "cart > discounts": "failed",
        "cart > later": "skipped",
        "standalone": "passed",
    }


def test_spec_failure_on_windows_with_a_file_url_location() -> None:
    result = parse_log(FIXTURES / "spec_fail_windows.log")
    (f,) = result.failures
    assert (f.full_name, f.file) == ("saves the order", "test/order.test.js")
    assert f.frames[0].path == "test/order.test.js"
    assert result.summary is not None and result.summary.failed == 1


def test_spec_pass_real_log_is_node_test_with_30_results() -> None:
    result = parse_log(LOGS / "other" / "puppeteer__puppeteer__121025524778.log")
    assert result.framework == "node-test" and result.failures == ()
    assert result.summary is not None and result.summary.passed == 30
    assert len(result.results) == 30
    assert "@puppeteer/ng-schematics: config > Single Project > should create default file" in {
        r.full_name for r in result.results
    }


# ---------------------------------------------------------------- detection


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tap_fail_windows.log", "node-test"),
        ("tap_pass.log", "node-test"),
        ("tap_nested.log", "node-test"),
        ("spec_fail.log", "node-test"),
    ],
)
def test_node_test_logs_are_detected_ahead_of_generic_tap(name: str, expected: str) -> None:
    found = detect(_lines(name))
    assert found[0].framework == expected
    assert "other" not in {d.framework for d in found}  # its TAP markers are its own


def test_plain_tap_stays_other() -> None:
    lines = [
        "TAP version 13",
        "ok 1 - a",
        "not ok 2 - b",
        "1..2",
        "# tests 2",
        "# pass 1",
        "# fail 1",
    ]
    assert detect(lines)[0].framework == "other"
    assert parse_log("\n".join(lines)).framework == "other"


def test_the_step_command_breaks_a_tie_toward_node_test() -> None:
    (d,) = detect(["ℹ tests 3"], commands=["node --test"])
    assert d.framework == "node-test" and d.command_match


@pytest.mark.parametrize(
    "path",
    sorted(p for p in LOGS.glob("*/*.log") if p.parent.name in ("jest", "mocha", "vitest")),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_existing_jest_mocha_vitest_fixtures_are_not_stolen(path: Path) -> None:
    result = parse_log(path)
    assert result.framework == path.parent.name or result.framework == "other"
    assert result.framework != "node-test"


def test_mocha_tick_lines_alone_do_not_make_a_node_test_log() -> None:
    assert detect(["  ✔ works", "  ✔ works too", "  3 passing (1s)"])[0].framework == "mocha"


# ---------------------------------------------------------------- triage


def test_triage_reads_the_real_demo_failure() -> None:
    real = Path(__file__).resolve().parents[3] / (
        "data/logs/Ranaweerakgls/piq-demo/41035868907/1/123996417612.log.gz"
    )
    text = (
        clean_file(real).text()
        if real.is_file()
        else clean_file(FIXTURES / "tap_fail_windows.log").text()
    )
    ev = read_log(text, "Ranaweerakgls/piq-demo")
    assert ev.framework == "node-test" and ev.is_test_failure
    (f,) = ev.failures
    assert f.file == "test/flaky-order.test.js"
    assert "Expected values to be strictly equal" in f.message
    assert ev.errors["assertion"] is True
    assert ev.n_failed_files == 1 and len(ev.test_ids) == 1


def test_node_test_encodes_as_all_zero_framework_columns_not_a_crash() -> None:
    lg = read_log(clean_file(FIXTURES / "tap_fail_windows.log").text(), "o/r")
    evidence = TriageEvidence(
        log=lg,
        overlap=tev.overlap(lg.failures, {"test/flaky-order.test.js": "@@ -5,2 +5,3 @@\n"}),
        history=tev.history_stats(len(lg.test_ids), []),
        same_sha=tev.same_sha_stats(1, "t", "ci.yml", [], 0.0),
        prerun_flaky=None,
    )
    row = feature_row(evidence)
    assert tuple(row) == FEATURE_NAMES  # the trained feature set is unchanged
    assert [row[f"lg_fw_{fw}"] for fw in FRAMEWORKS] == [0.0, 0.0, 0.0]
    assert row["er_assertion"] == 1.0 and row["lg_n_failed_tests"] == 1.0
