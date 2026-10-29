"""P4.9 evidence: error families, stack versus diff, history and same-SHA counts (no database)."""

import math

from testing_expert.parsing import FailedTest, StackFrame
from testing_expert.triage import evidence as ev
from testing_expert.triage.features import (
    FEATURE_NAMES,
    GROUPS,
    TriageEvidence,
    feature_row,
    read_log,
    reasons,
)


def frame(path: str | None, line: int | None = 10, kind: str = "project") -> StackFrame:
    return StackFrame(None, "at x", path, line, 1, kind)  # type: ignore[arg-type]


def failure(
    file: str | None, frames: tuple[StackFrame, ...] = (), message: str = "boom"
) -> FailedTest:
    return FailedTest("a > b", ("a", "b"), file, "header", "Error", message, frames=frames)


# --- error families ---------------------------------------------------------------------------


def test_error_families_match_their_regexes() -> None:
    cases = {
        "assertion": "AssertionError: expected 1 to equal 2",
        "snapshot": "Snapshot name: `a 1` mismatched",
        "code_error": "TypeError: Cannot read properties of undefined (reading 'x')",
        "compile": "error TS2345: Argument of type",
        "module_missing": "Error: Cannot find module './x'",
        "timeout": "Error: Exceeded timeout of 5000 ms for a test",
        "conn": "Error: read ECONNRESET",
        "addrinuse": "Error: listen EADDRINUSE: address already in use :::3000",
        "network": "getaddrinfo ENOTFOUND registry.npmjs.org",
        "oom": "FATAL ERROR: JavaScript heap out of memory",
        "fs_resource": "ENOENT: no such file or directory, open 'x'",
        "killed": "Process completed with exit code 137.",
        "unhandled": "UnhandledPromiseRejection: This error originated",
    }
    assert set(cases) == set(ev.ERROR_FAMILIES)
    for family, text in cases.items():
        assert ev.classify_errors(text)[family], family


def test_clean_text_matches_nothing_and_tail_is_searched() -> None:
    assert not any(ev.classify_errors("all good").values())
    assert ev.classify_errors("", tail="x" * 10 + "\nSIGKILL")["killed"]
    # only the last TAIL_CHARS of the tail count
    assert not ev.classify_errors("", tail="SIGKILL" + "x" * (ev.TAIL_CHARS + 5))["killed"]


# --- stack versus diff --------------------------------------------------------------------------


def test_hunk_ranges_and_path_matching() -> None:
    patch = "@@ -1,2 +10,3 @@\n a\n@@ -30 +40 @@\n"
    assert ev.hunk_ranges(patch) == [(10, 12), (40, 40)]
    assert ev.hunk_ranges(None) == []
    assert ev.path_matches("src/x.ts", "packages/p/src/x.ts")
    assert ev.path_matches("packages/p/src/x.ts", "src/x.ts")
    assert not ev.path_matches("src/x.ts", "src/xx.ts")
    assert not ev.path_matches("", "a.ts")


def test_overlap_file_and_line_level() -> None:
    f = failure("test/a.test.ts", (frame("test/a.test.ts", 5), frame("src/lib.ts", 52)))
    changed = {"src/lib.ts": "@@ -45,3 +50,4 @@\n", "docs/readme.md": None}
    o = ev.overlap([f], changed)
    assert o.commit_files_known and o.n_changed_files == 2
    assert o.test_file_changed is False
    assert o.stack_files_total == 1 and o.stack_files_changed == 1
    assert o.stack_source_changed is True
    assert o.frame_in_hunk is True and o.first_hunk_frame == ("src/lib.ts", 52)
    assert o.touches_changed_code
    # the same frame 20 lines away from the hunk is file-level only
    far = failure("test/a.test.ts", (frame("src/lib.ts", 200),))
    o2 = ev.overlap([far], changed)
    assert o2.stack_files_changed == 1 and o2.frame_in_hunk is False


def test_overlap_test_file_changed_and_unknown_commit() -> None:
    f = failure("test/a.test.ts", (frame("src/other.ts", 1),))
    o = ev.overlap([f], {"test/a.test.ts": None})
    assert o.test_file_changed is True and o.frame_in_hunk is None  # no patch: line level unknown
    unknown = ev.overlap([f], None)
    assert not unknown.commit_files_known
    assert unknown.test_file_changed is None and unknown.stack_files_changed is None
    assert not unknown.touches_changed_code


def test_overlap_ignores_dependency_frames() -> None:
    f = failure(
        "a.test.ts",
        (frame("node_modules/x/i.js", 3, "dependency"), frame(None, None, "node_internal")),
    )
    o = ev.overlap([f], {"node_modules/x/i.js": None})
    assert o.stack_files_total == 0 and o.stack_files_changed is None


# --- history ---------------------------------------------------------------------------------


def test_history_stats() -> None:
    recs = [ev.PriorTest("a", 10, 0, 0, 0), ev.PriorTest("b", 20, 5, 4, 2)]
    h = ev.history_stats(3, recs)
    assert h.known and h.frac_new == 1 / 3 and h.frac_never_failed == 1 / 3
    assert h.max_fail_rate == 0.25 and math.isclose(h.max_flip_rate or 0, 4 / 19)
    assert h.max_mixed_shas == 2 and h.worst_test_id == "b"
    none = ev.history_stats(2, [])
    assert not none.known and none.frac_new == 1.0 and none.max_fail_rate is None
    assert ev.history_stats(0, []).frac_new is None


# --- same SHA --------------------------------------------------------------------------------


def sib(name: str, ok: bool, ts: float, wf: str = "ci.yml", attempt: int = 1) -> ev.SiblingJob:
    return ev.SiblingJob(1, name, wf, attempt, "success" if ok else "failure", ts)


def test_same_sha_counts_only_jobs_finished_by_the_cutoff() -> None:
    sibs = [
        sib("Tests (18)", True, 90.0),  # same job name? no: another matrix leg
        sib("Tests (20)", True, 80.0),  # the failing job's own name, earlier attempt: passed
        sib("Tests (20)", False, 70.0, attempt=2),  # same job failed before
        sib("lint", True, 60.0),
        sib("lint", False, 50.0, wf="other.yml"),
        sib("Tests (20)", True, 100.0),  # finishes exactly at the cutoff: counted
        sib("Tests (20)", True, 100.5),  # finishes AFTER the cutoff: never counted
        sib("Tests (22)", True, 5000.0),
    ]
    s = ev.same_sha_stats(2, "Tests (20)", "ci.yml", sibs, cutoff_ts=100.0)
    assert (s.same_job_pass, s.same_job_fail) == (2, 1)
    assert (s.family_pass, s.family_fail) == (1, 0)
    assert (s.other_pass, s.other_fail) == (1, 1)
    assert s.attempt == 2 and s.first_pass_job


def test_family_key() -> None:
    assert ev.family_key("Tests (node 22, ubuntu)") == "tests"
    assert ev.family_key(None) == ""


# --- features and reasons ---------------------------------------------------------------------

VITEST_LOG = """\
 FAIL  src/__tests__/a.test.ts > suite > does a thing
AssertionError: expected 1 to be 2
 ❯ src/__tests__/a.test.ts:5:10
 ❯ src/lib.ts:52:3

 Test Files  1 failed (1)
      Tests  1 failed | 3 passed (4)
"""


def evidence_for_log(**kw: object) -> TriageEvidence:
    lg = read_log(VITEST_LOG, "o/r")
    return TriageEvidence(
        log=lg,
        overlap=ev.overlap(lg.failures, {"src/lib.ts": "@@ -50,2 +50,3 @@\n"}),
        history=ev.history_stats(len(lg.test_ids), []),
        same_sha=ev.same_sha_stats(1, "t", "ci.yml", [], 0.0),
        prerun_flaky=kw.get("prerun"),  # type: ignore[arg-type]
    )


def test_feature_row_has_every_feature_and_nan_for_unknown() -> None:
    row = feature_row(evidence_for_log())
    assert tuple(row) == FEATURE_NAMES
    assert math.isnan(row["pr_flaky_risk"])  # no pre-run risk: unknown, not zero
    assert row["er_assertion"] == 1.0
    assert row["lg_fw_vitest"] == 1.0
    assert set().union(*GROUPS.values()) <= set(FEATURE_NAMES)


def test_every_evidence_item_becomes_a_reason() -> None:
    items = reasons(evidence_for_log(prerun=0.4))
    sources = {r.source for r in items}
    assert {"stack_trace", "log", "history", "same_sha", "pre_run"} <= sources
    stack = next(r for r in items if r.source == "stack_trace")
    assert stack.path == "src/lib.ts" and stack.line == 52 and stack.leans == "genuine"
    assert any("40%" in r.text for r in items if r.source == "pre_run")
    assert all(0 < len(r.text) <= 1000 for r in items)
