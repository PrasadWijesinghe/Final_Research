"""Stack-frame parsing: every path form seen in real GitHub Actions logs."""

from __future__ import annotations

import pytest
from testing_expert.parsing import StackFrame, parse_frame, parse_frames

# (line, function, path, line number, column, kind)
FRAME_CASES: list[tuple[str, str | None, str | None, int | None, int | None, str]] = [
    # relative path (jest, mocha)
    (
        "      at getUsageInBody (packages/x/src/loop.ts:54:57)",
        "getUsageInBody",
        "packages/x/src/loop.ts",
        54,
        57,
        "project",
    ),
    # Linux runner workspace, repo name repeated
    (
        "    at Object.<anonymous> (/home/runner/work/jest/jest/packages/jest-core/src/a.ts:10:5)",
        "Object.<anonymous>",
        "packages/jest-core/src/a.ts",
        10,
        5,
        "project",
    ),
    # macOS runner, bare `at loc`
    (
        "    at /Users/runner/work/babel/babel/packages/babel-core/src/x.js:1:2",
        None,
        "packages/babel-core/src/x.js",
        1,
        2,
        "project",
    ),
    # Windows backslashes and drive letter
    (
        r"    at Object.<anonymous> (D:\a\jest\jest\e2e\__tests__\x.test.js:3:9)",
        "Object.<anonymous>",
        "e2e/__tests__/x.test.js",
        3,
        9,
        "project",
    ),
    # lower-case drive letter
    (
        r"    at run (d:\a\knex\knex\lib\client.js:77:11)",
        "run",
        "lib/client.js",
        77,
        11,
        "project",
    ),
    # file:// URL with drive letter (ESM), async prefix
    (
        "    at async Context.<anonymous> (file:///D:/a/puppeteer/puppeteer/test/src/x.spec.ts:20:3)",
        "Context.<anonymous>",
        "test/src/x.spec.ts",
        20,
        3,
        "project",
    ),
    # file:// URL on Linux
    (
        "    at file:///home/runner/work/vite/vite/packages/vite/src/node/a.ts:5:6",
        None,
        "packages/vite/src/node/a.ts",
        5,
        6,
        "project",
    ),
    # webpack:// source map path
    (
        "    at Linter.verify (webpack://eslint/./lib/linter/linter.js:10:2)",
        "Linter.verify",
        "lib/linter/linter.js",
        10,
        2,
        "project",
    ),
    # cache-busting query string before the line number
    (
        "    at Context.<anonymous> (test/foo.test.js?1792496055046:168:34)",
        "Context.<anonymous>",
        "test/foo.test.js",
        168,
        34,
        "project",
    ),
    # Deno's built-ins print as `ext:...` (mongoose runs its tests under Deno)
    (
        "    at new AssertionError (ext:deno_node/assertion_error.ts:412:11)",
        "AssertionError",
        None,
        412,
        11,
        "node_internal",
    ),
    # node internals, both spellings
    (
        "    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)",
        "process.processTicksAndRejections",
        None,
        95,
        5,
        "node_internal",
    ),
    (
        "    at Module._compile (internal/modules/cjs/loader.js:1063:30)",
        "Module._compile",
        None,
        1063,
        30,
        "node_internal",
    ),
    # `at async <location>` and `at new <location>` have no function: the keyword is not the path
    (
        "    at async file:///home/runner/work/R/R/node_modules/tinypool/dist/entry/worker.js:72:20",
        None,
        "node_modules/tinypool/dist/entry/worker.js",
        72,
        20,
        "dependency",
    ),
    ("    at async /home/runner/work/R/R/e2e/x.js:1:2", None, "e2e/x.js", 1, 2, "project"),
    ("    at new /home/runner/work/R/R/src/a.js:3:4", None, "src/a.js", 3, 4, "project"),
    ("    at async new src/a.js:5:6", None, "src/a.js", 5, 6, "project"),
    # a path above the directory the tests ran from has no repo-relative form
    ("    at f (../scope-manager/src/a.ts:6:43)", "f", None, 6, 43, "unknown"),
    ("    at ..\\lib\\a.ts:6:43", None, None, 6, 43, "unknown"),
    (
        " ❯ Object.<anonymous> ../scope-manager/src/a.ts:6:43",
        "Object.<anonymous>",
        None,
        6,
        43,
        "unknown",
    ),
    # browser URLs (vitest browser mode) are not files in the repo
    ("    at f (http://localhost:3000/src/x.js?v=1:168:34)", "f", None, 168, 34, "unknown"),
    ("    at https://cdn.example.com/lib.js:1:2", None, None, 1, 2, "unknown"),
    # placeholders
    ("    at new Promise (<anonymous>)", "Promise", None, None, None, "unknown"),
    ("    at Array.map (<anonymous>)", "Array.map", None, None, None, "unknown"),
    ("    at async Promise.all (index 0)", "Promise.all", None, None, None, "unknown"),
    # dependencies
    (
        "    at Object.<anonymous> (/home/runner/work/x/x/node_modules/foo/lib/index.js:1:1)",
        "Object.<anonymous>",
        "node_modules/foo/lib/index.js",
        1,
        1,
        "dependency",
    ),
    (
        "    at x (/opt/hostedtoolcache/node/22/x64/lib/node_modules/npm/lib/cli.js:3:4)",
        "x",
        "node_modules/npm/lib/cli.js",
        3,
        4,
        "dependency",
    ),
    # temp paths are not project code
    ("    at Foo (/tmp/xyz/foo.js:1:2)", "Foo", None, 1, 2, "unknown"),
    (
        r"    at x (C:\Users\RUNNER~1\AppData\Local\Temp\jest_abc\a.js:1:2)",
        "x",
        None,
        1,
        2,
        "unknown",
    ),
    # an absolute path outside the checkout stays as it is, but is not "project"
    ("    at x (/opt/other/a.js:9:9)", "x", "/opt/other/a.js", 9, 9, "unknown"),
    # method alias and `new`
    ("    at new Foo [as bar] (src/a.js:3:4)", "Foo", "src/a.js", 3, 4, "project"),
    # eval frames: the last part is where the code ran
    (
        "    at eval (eval at <anonymous> (file.js:1:2), <anonymous>:3:4)",
        "eval",
        None,
        3,
        4,
        "unknown",
    ),
    # vitest `❯` lines
    (" ❯ src/foo.ts:12:3", None, "src/foo.ts", 12, 3, "project"),
    (
        " ❯ Object.test packages/svelte/tests/_config.js:44:10",
        "Object.test",
        "packages/svelte/tests/_config.js",
        44,
        10,
        "project",
    ),
    (" ❯ Object.test .../_config.js:44:10", "Object.test", None, 44, 10, "unknown"),
    # `./` prefix is dropped
    ("    at f (./src/a.js:1:1)", "f", "src/a.js", 1, 1, "project"),
]

NOT_FRAMES = [
    "",
    "    at least one assertion must pass",
    "    at the end of the day (see the docs)",
    " ❯ test/a.test.ts (3 tests | 1 failed) 12ms",
    " ❯ test/a.test.ts  (3 tests | 1 failed)",
    "Error: boom",
    "    at",
    "    at   ",
    "random text with at in the middle at x:1:2",
    "x" * 5000,
    "    at " + "a" * 5000 + ":1:2",
]


@pytest.mark.parametrize(("line", "function", "path", "lineno", "col", "kind"), FRAME_CASES)
def test_parse_frame(
    line: str,
    function: str | None,
    path: str | None,
    lineno: int | None,
    col: int | None,
    kind: str,
) -> None:
    frame = parse_frame(line)
    assert frame is not None, line
    assert (frame.function, frame.path, frame.line, frame.col, frame.kind) == (
        function,
        path,
        lineno,
        col,
        kind,
    )
    assert frame.raw == line.strip()


@pytest.mark.parametrize("line", NOT_FRAMES)
def test_not_a_frame(line: str) -> None:
    assert parse_frame(line) is None


def test_parse_frames_keeps_order_and_skips_other_lines() -> None:
    lines = [
        "TypeError: x is not a function",
        "    at a (src/a.js:1:1)",
        "",
        "    at b (src/b.js:2:2)",
    ]
    frames = parse_frames(lines)
    assert [f.function for f in frames] == ["a", "b"]
    assert all(isinstance(f, StackFrame) for f in frames)


def test_raw_is_capped() -> None:
    line = "    at f (src/" + "d/" * 400 + "a.js:1:1)"  # 810 chars: under the line limit
    frame = parse_frame(line)
    assert frame is not None
    assert len(frame.raw) <= 300
