"""Build labelling rules on real failure logs (docs/experts/build.md section 3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from build_expert.labels import JobFacts, StepFacts, decide, is_build_command, to_failure_label
from build_expert.parsing import parse_job_log

from shared.contracts import FailureCategory

LOGS = Path(__file__).parent / "fixtures" / "logs"
SETUP = (
    StepFacts(1, "Set up job", "success"),
    StepFacts(2, "Run actions/checkout@v4", "success"),
    StepFacts(3, "Run actions/setup-node@v4", "success"),
)


def _job(failed_step: str | None, conclusion: str = "failure", **kw: object) -> JobFacts:
    steps = SETUP + ((StepFacts(4, failed_step, "failure"),) if failed_step else ())
    return JobFacts(
        repo="acme/acme-web",
        run_id=1,
        attempt=1,
        job_id=11,
        conclusion=conclusion,
        steps=steps,
        **kw,  # type: ignore[arg-type]
    )


def _signals(name: str):  # type: ignore[no-untyped-def]
    return parse_job_log((LOGS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("fixture", "step", "sub", "tool"),
    [
        ("tsc_typecheck_fail.log", "Run pnpm run typecheck", "tsc", "tsc"),
        ("tsc_pretty_build_fail.log", "Run npm run build", "tsc", "tsc"),
        ("eslint_fail.log", "Run pnpm run lint", "eslint", "eslint"),
        ("eslint_max_warnings.log", "Run npm run lint:ci", "eslint", "eslint"),
        ("prettier_fail.log", "Run npm run format:check", "format", "other"),
        ("vite_build_fail.log", "Run npm run build", "bundler", "bundler"),
        ("webpack_build_fail.log", "Run npm run build:wp", "bundler", "bundler"),
        ("esbuild_fail.log", "Run npx esbuild src/main.js --bundle", "bundler", "bundler"),
    ],
)
def test_build_failures_are_claimed_with_high_confidence(
    fixture: str, step: str, sub: str, tool: str
) -> None:
    d = decide(_job(step), _signals(fixture))
    assert d.claimed
    assert (d.rule_name, d.sub_label, d.confidence, d.tool) == (
        "build_step_tool_error",
        sub,
        "high",
        tool,
    )


@pytest.mark.parametrize(
    ("fixture", "step", "rule"),
    [
        ("dependency_eresolve.log", "Run npm install", "not_build_setup_step"),
        ("dependency_eresolve.log", "Install", "not_build_setup_step"),
        ("dependency_eresolve.log", "Resolve packages", "not_build_dependency"),
        ("heap_oom_build.log", "Run npm run build", "not_build_resource"),
        ("vitest_test_fail.log", "Run pnpm exec vitest run", "not_build_test_failure"),
    ],
)
def test_other_categories_are_not_claimed(fixture: str, step: str, rule: str) -> None:
    d = decide(_job(step), _signals(fixture))
    assert not d.claimed
    assert d.rule_name == rule


def test_tool_error_in_a_step_that_is_not_a_build_step_is_medium() -> None:
    d = decide(_job("Run ./scripts/ci.sh"), _signals("eslint_fail.log"))
    assert d.claimed and d.rule_name == "build_step_tool_error"  # the log's title is the command
    renamed = _signals("eslint_fail.log")
    renamed.failed_steps[0].title = "./scripts/ci.sh"
    d2 = decide(_job("Run ./scripts/ci.sh"), renamed)
    assert (d2.rule_name, d2.confidence) == ("tool_error_other_step", "medium")


def test_build_step_without_a_known_tool_is_build_other() -> None:
    signals = parse_job_log(
        "2026-10-14T10:00:00.0000000Z ##[group]Run npm run build\n"
        "2026-10-14T10:00:00.0000000Z npm run build\n"
        "2026-10-14T10:00:00.0000000Z ##[endgroup]\n"
        "2026-10-14T10:00:01.0000000Z Error: custom codegen script failed\n"
        "2026-10-14T10:00:01.0000000Z ##[error]Process completed with exit code 1.\n"
    )
    d = decide(_job("Run npm run build"), signals)
    assert (d.claimed, d.rule_name, d.sub_label, d.confidence) == (
        True,
        "build_step_no_signature",
        "build_other",
        "medium",
    )


def test_rerun_pass_lowers_confidence_and_adds_a_note() -> None:
    d = decide(_job("Run npm run build", passed_on_rerun=True), _signals("vite_build_fail.log"))
    assert d.claimed and d.confidence == "medium"
    assert d.notes is not None and "re-run" in d.notes


def test_no_log_uses_the_step_name_with_low_confidence() -> None:
    d = decide(_job("Run pnpm run typecheck"), None)
    assert (d.claimed, d.rule_name, d.sub_label, d.confidence) == (
        True,
        "build_step_name_only",
        "tsc",
        "low",
    )
    assert not decide(_job("Run pnpm test"), None).claimed
    assert not decide(_job(None), None).claimed


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", "timed_out"])
def test_only_failures_are_considered(conclusion: str) -> None:
    d = decide(_job("Run pnpm run lint", conclusion=conclusion), _signals("eslint_fail.log"))
    assert not d.claimed


def test_log_without_exit_marker_reads_the_whole_log_with_less_trust() -> None:
    text = (LOGS / "eslint_fail.log").read_text(encoding="utf-8")
    cut = "\n".join(ln for ln in text.splitlines() if "Process completed" not in ln)
    d = decide(_job("Run pnpm run lint"), parse_job_log(cut))
    assert d.claimed and d.rule_name == "build_step_tool_error_whole_log"
    assert d.confidence == "medium"


def test_failure_label_contract() -> None:
    job = _job("Run pnpm run typecheck")
    label = to_failure_label(job, decide(job, _signals("tsc_typecheck_fail.log")))
    assert label.category is FailureCategory.BUILD
    assert label.sub_label == "tsc"
    assert label.labelled_by == "rule"
    assert label.rule_name == "build.build_step_tool_error.v1"
    assert (label.repo.full_name, label.run_id, label.job_id) == ("acme/acme-web", 1, 11)
    with pytest.raises(ValueError):
        to_failure_label(job, decide(_job("Run npm install"), None))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Run pnpm run typecheck", True),
        ("Lint", True),
        ("Build and bundle", True),
        ("Check formatting", True),
        ("Run npm run format:check", True),
        ("Run npm ci", False),
        ("Install dependencies", False),
        ("Run tests", False),
        ("Run pnpm run test:types", True),
        (None, False),
    ],
)
def test_is_build_command(text: str | None, expected: bool) -> None:
    assert is_build_command(text) is expected
