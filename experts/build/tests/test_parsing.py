"""Log parsing on real tool output (fixtures/logs, see its README) and on small edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest
from build_expert.parsing import clean_line, find_hints, parse_job_log, parse_tools, split_sections
from build_expert.parsing.tools import MAX_LOCATIONS, repo_relative, strip_task_prefix

LOGS = Path(__file__).parent / "fixtures" / "logs"


def _log(name: str) -> str:
    return (LOGS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fixture", "step", "tool", "errors", "warnings", "codes"),
    [
        ("tsc_typecheck_fail.log", "pnpm run typecheck", "tsc", 7, 0, {"TS6133": 3, "TS2322": 2}),
        ("tsc_pretty_build_fail.log", "npm run build", "tsc", 7, 0, {"TS2307": 1, "TS18048": 1}),
        ("eslint_fail.log", "pnpm run lint", "eslint", 4, 1, {"eqeqeq": 1, "no-console": 1}),
        ("eslint_max_warnings.log", "npm run lint:ci", "eslint", 0, 2, {"no-console": 2}),
        ("prettier_fail.log", "npm run format:check", "prettier", 2, 0, {"unformatted_file": 2}),
        # Real CI log (langchainjs, 30 Sep 2026): oxfmt, the oxc formatter.
        (
            "oxfmt_check_fail.log",
            "pnpm run format:check",
            "prettier",
            1,
            0,
            {"unformatted_file": 1},
        ),
        ("vite_build_fail.log", "npm run build", "bundler", 1, 0, {"unresolved_import": 1}),
        ("webpack_build_fail.log", "npm run build:wp", "bundler", 1, 0, {"unresolved_import": 1}),
        ("esbuild_fail.log", "npx esbuild", "bundler", 1, 0, {"unresolved_import": 1}),
    ],
)
def test_failed_step_and_tool_on_real_output(
    fixture: str, step: str, tool: str, errors: int, warnings: int, codes: dict[str, int]
) -> None:
    signals = parse_job_log(_log(fixture))
    failed = signals.failed_steps
    assert len(failed) == 1
    assert failed[0].title.startswith(step)
    findings = [f for f in failed[0].findings if f.tool == tool]
    assert len(findings) == 1
    f = findings[0]
    assert (f.errors, f.warnings) == (errors, warnings)
    for code, n in codes.items():
        assert f.codes[code] == n
    assert f.failed


def test_bundler_names_and_locations() -> None:
    vite = parse_job_log(_log("vite_build_fail.log")).failed_steps[0].findings[0]
    webpack = parse_job_log(_log("webpack_build_fail.log")).failed_steps[0].findings[0]
    esbuild = parse_job_log(_log("esbuild_fail.log")).failed_steps[0].findings[0]
    assert (vite.bundler, webpack.bundler, esbuild.bundler) == ("vite", "webpack", "esbuild")
    assert webpack.locations[0].path == "src/main.js" and webpack.locations[0].line == 2
    assert (esbuild.locations[0].path, esbuild.locations[0].line) == ("src/main.js", 2)
    assert vite.locations[0].path == "src/main.js"


def test_tsc_locations_are_repo_relative_with_codes() -> None:
    f = parse_job_log(_log("tsc_pretty_build_fail.log")).failed_steps[0].findings[0]
    first = f.locations[0]
    assert (first.path, first.line, first.column, first.code) == ("src/api.ts", 2, 1, "TS6133")
    assert {loc.path for loc in f.locations} == {"src/api.ts", "src/lint.ts", "src/user.ts"}


def test_eslint_max_warnings_is_a_failure_without_errors() -> None:
    f = parse_job_log(_log("eslint_max_warnings.log")).failed_steps[0].findings[0]
    assert f.errors == 0 and f.max_warnings_exceeded and f.failed
    assert f.locations[0].path == "src/warn.ts"


def test_passing_log_keeps_warning_counts() -> None:
    signals = parse_job_log(_log("lint_pass_with_warnings.log"))
    assert signals.failed_steps == []
    assert signals.lint_warnings == 2 and signals.lint_errors == 0
    assert all(not f.failed for s in signals.steps for f in s.findings)


def test_earlier_passing_step_is_not_the_failed_one() -> None:
    signals = parse_job_log(_log("tsc_typecheck_fail.log"))
    assert [s.title for s in signals.failed_steps] == ["pnpm run typecheck"]
    assert signals.lint_warnings == 2  # from the passing lint step before it
    assert signals.type_errors == 7


@pytest.mark.parametrize(
    ("fixture", "hint"),
    [
        ("dependency_eresolve.log", "dependency"),
        ("heap_oom_build.log", "resource"),
        ("vitest_test_fail.log", "test"),
    ],
)
def test_non_build_failures_give_hints_and_no_tool(fixture: str, hint: str) -> None:
    failed = parse_job_log(_log(fixture)).failed_steps[0]
    assert hint in failed.hints
    assert not any(f.failed for f in failed.findings)


def test_split_sections_strips_framing() -> None:
    sections = split_sections(_log("eslint_fail.log"))
    titles = [s.title for s in sections]
    assert titles[:5] == [
        "",
        "actions/checkout@v4",
        "actions/setup-node@v4",
        "pnpm install --frozen-lockfile",
        "pnpm run lint",
    ]
    lint = sections[4]
    assert lint.failed and lint.exit_code == 1 and not lint.is_action
    assert sections[1].is_action
    assert not any(ln.startswith("20") or "##[" in ln for ln in lint.lines)
    assert sections[-1].title == "post"


def test_clean_line_removes_timestamp_and_ansi() -> None:
    raw = "2026-10-14T10:02:44.1230000Z \x1b[96msrc/a.ts\x1b[0m:\x1b[93m2\x1b[0m - x  "
    assert clean_line(raw) == "src/a.ts:2 - x"


def test_monorepo_prefixes_are_stripped() -> None:
    lines = [
        "packages/web typecheck: src/a.ts(3,4): error TS2322: Type 'x' is not assignable.",
        "@acme/ui:lint: /home/runner/work/r/r/packages/ui/src/b.ts",
        "@acme/ui:lint:   1:2  error  Unexpected any  @typescript-eslint/no-explicit-any",
        "@acme/ui:lint: ✖ 1 problem (1 error, 0 warnings)",
    ]
    assert strip_task_prefix(lines[0]).startswith("src/a.ts(3,4)")
    found = {f.tool: f for f in parse_tools(lines)}
    assert found["tsc"].errors == 1
    assert found["eslint"].errors == 1
    assert found["eslint"].locations[0].path == "packages/ui/src/b.ts"


@pytest.mark.parametrize(
    ("line", "code", "config"),
    [
        ("error TS5023: Unknown compiler option 'strictt'.", "TS5023", True),
        (
            "tsconfig.json(3,5): error TS5024: Compiler option 'strict' requires a value.",
            "TS5024",
            True,
        ),
        ("Type error: Property 'x' does not exist on type 'Y'.", "next_type_error", False),
    ],
)
def test_tsc_global_and_config_errors(line: str, code: str, config: bool) -> None:
    (f,) = parse_tools([line])
    assert f.tool == "tsc" and f.codes[code] == 1 and f.config_error is config


def test_eslint_crash_is_a_config_failure() -> None:
    (f,) = parse_tools(["", "Oops! Something went wrong! :(", "", "ESLint: 9.39.5"])
    assert f.tool == "eslint" and f.config_error and f.failed


def test_next_failed_to_compile_without_type_error_is_a_bundler_failure() -> None:
    (f,) = parse_tools(["Failed to compile.", "", "./src/app/page.tsx"])
    assert (f.tool, f.bundler, f.errors) == ("bundler", "next", 1)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/home/runner/work/acme-web/acme-web/src/a.ts", "src/a.ts"),
        ("D:\\a\\acme-web\\acme-web\\src\\a.ts", "src/a.ts"),
        ("./src/a.ts", "src/a.ts"),
        ("file:///home/runner/work/x/x/node_modules/rollup/x.js", "node_modules/rollup/x.js"),
    ],
)
def test_repo_relative(path: str, expected: str) -> None:
    assert repo_relative(path) == expected


def test_plain_text_finds_nothing() -> None:
    lines = ["Run npm ci", "added 412 packages in 9s", "Done", "Found 0 errors. Watching."]
    assert parse_tools(lines) == []
    assert find_hints(lines) == set()


def test_huge_lint_output_keeps_counts_but_caps_locations() -> None:
    lines = ["/home/runner/work/r/r/src/big.ts"]
    lines += [f"  {i}:1  warning  Unexpected console statement  no-console" for i in range(1, 501)]
    lines += ["", "✖ 500 problems (0 errors, 500 warnings)"]
    (f,) = parse_tools(lines)
    assert f.warnings == 500 and f.codes["no-console"] == 500
    assert len(f.locations) == MAX_LOCATIONS


def test_oxfmt_file_is_located() -> None:
    f = parse_job_log(_log("oxfmt_check_fail.log")).failed_steps[0].findings[0]
    assert f.locations[0].path == (
        "libs/providers/langchain-openai/src/converters/tests/responses.test.ts"
    )


def test_custom_lint_script_has_no_known_tool() -> None:
    # Real CI log (prisma, 18 Sep 2026): a repo-specific lint script, not ESLint.
    failed = parse_job_log(_log("custom_lint_script_fail.log")).failed_steps
    assert failed[0].title == "pnpm lint:framework-vocabulary"
    assert failed[0].findings == []
