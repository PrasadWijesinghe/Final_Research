"""Parses one stack-trace line into a `StackFrame`, and maps its path to a repo-relative path.

Two line shapes are understood:
- V8 (Node): `at fn (loc)`, `at async fn (loc)`, `at new Foo (loc)`, `at fn [as x] (loc)`,
  and the bare `at loc`.
- Vitest: ` ❯ fn loc` and ` ❯ loc` (only when the location has a line number).

`loc` looks like `path:line:col`. It is read from the right because Windows paths contain a colon
(`D:\\a\\repo\\x.js:3:9`). Path forms seen in real logs: relative, `/home/runner/work/R/R/...`,
`/Users/runner/work/R/R/...`, `D:\\a\\R\\R\\...`, `file:///D:/a/...`, `webpack://name/./...`,
`...test.js?1792496055046:168:34`, `node:internal/...` and `<anonymous>`.

A path is relative to the directory the tests ran from, which is the repo root only when the
runner started there (a monorepo package prints `src/x.ts` for `packages/p/src/x.ts`). A path
that starts with `../` or is a browser URL (`http://...`) has no usable location: its kind is
`unknown` and `path` is None (the original text stays in `raw`).

Whether the file exists in the commit is not checked here (P4.3 and P4.9 do that).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from testing_expert.parsing.models import FrameKind, StackFrame
from testing_expert.parsing.settings import ParsingConfig, StackSettings, load_config

# Both line patterns are used on a right-stripped line, so no trailing `\s*$` (a lazy `.*?` before
# it would be quadratic on a line of spaces).
_V8_LINE = re.compile(r"^\s*at\s+(?P<rest>\S.*)$")
_VITEST_LINE = re.compile(r"^\s*\u276f\s+(?P<rest>\S.*)$")
_LOCATION = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<col>\d+))?$")
_WEBPACK = re.compile(r"^webpack(?:-internal)?://[^/]*/(?:\./)?")
# A location is plausible when it ends in :line[:col] or is one of V8's placeholders.
_PLAUSIBLE = re.compile(
    r":\d+(?::\d+)?$|^<anonymous>$|^native$|^index \d+$|^node:|^\[native code\]$"
    r"|^unknown location$|^eval at "
)
_DRIVE_AFTER_FILE = re.compile(r"^/[A-Za-z]:/")


def _kind_and_path(raw_path: str, cfg: StackSettings) -> tuple[str | None, FrameKind]:
    """Map a raw location path to (repo-relative path or None, kind)."""
    path = raw_path.strip()
    if not path or path in {"<anonymous>", "native", "[native code]", "unknown location"}:
        return None, "unknown"
    if path.startswith(("index ", "eval at ", "http://", "https://")):
        return None, "unknown"  # a browser URL (vitest browser mode) is not a file in the repo
    if path.startswith("file://"):
        path = path[len("file://") :]
        if _DRIVE_AFTER_FILE.match(path):
            path = path[1:]
    path = _WEBPACK.sub("", path, count=1)
    for sep in ("?", "#"):
        path = path.split(sep, 1)[0]
    path = path.replace("\\", "/")
    for pattern in cfg.node_internal_patterns:
        if pattern.match(path):
            return None, "node_internal"
    if cfg.elided_path.match(path):
        return None, "unknown"
    for pattern in cfg.temp_paths:
        if pattern.match(path):
            return None, "unknown"
    for pattern in cfg.workspace_prefixes:
        stripped = pattern.sub("", path, count=1)
        if stripped != path:
            path = stripped
            break
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("../"):
        # `../x` means the tests ran from a package folder and the file is outside it: the path
        # cannot be made relative to the repo root without knowing that folder.
        return None, "unknown"
    marker = cfg.dependency_marker
    if marker in path:
        if path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
            path = path[path.index(marker) :]  # drop the machine's absolute prefix
        return path, "dependency"
    if path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        return path, "unknown"  # an absolute path outside the repo checkout
    return path, "project"


def map_path(raw_path: str, config: ParsingConfig | None = None) -> tuple[str | None, FrameKind]:
    """Map a path seen in a log to (repo-relative path or None, kind), as stack frames do."""
    return _kind_and_path(raw_path, (config or load_config()).stack)


def _frame(
    function: str | None, loc: str, line: str, cfg: StackSettings, *, bare: bool
) -> StackFrame | None:
    loc = loc.strip()
    if loc.startswith("eval at "):
        loc = loc.rsplit(", ", 1)[-1]  # the last part is where the eval'd code ran
    if not _PLAUSIBLE.search(loc):
        return None
    m = _LOCATION.match(loc)
    if m:
        raw_path, lineno, col = m.group("path"), int(m.group("line")), m.group("col")
    elif bare and not loc.startswith("node:"):
        return None  # `at <word>` with no line number is prose, not a frame
    else:
        raw_path, lineno, col = loc, None, None
    path, kind = _kind_and_path(raw_path, cfg)
    return StackFrame(
        function=function or None,
        raw=line.strip()[: cfg.max_raw_chars],
        path=path,
        line=lineno,
        col=int(col) if col else None,
        kind=kind,
    )


def _strip_call_prefixes(text: str) -> str:
    """Remove leading `async ` and `new ` (`at async fn (loc)`, `at new Foo (loc)`)."""
    text = text.lstrip()
    while text.startswith(("async ", "new ")):
        text = text.split(" ", 1)[1].lstrip()
    return text


def _clean_function(name: str) -> str | None:
    name = name.strip()
    if name.endswith("]"):  # `fn [as alias]`
        cut = name.rfind(" [as ")
        if cut > 0:
            name = name[:cut]
    return _strip_call_prefixes(name).strip() or None


def parse_frame(line: str, config: ParsingConfig | None = None) -> StackFrame | None:
    """Parse one log line as a stack frame, or return None when it is not one. Never raises."""
    cfg = (config or load_config()).stack
    try:
        if len(line) > cfg.max_line_chars:
            return None
        line = line.rstrip()
        m = _V8_LINE.match(line)
        if m:
            rest = m.group("rest")
            if rest.endswith(")") and " (" in rest:
                cut = rest.index(" (")
                return _frame(
                    _clean_function(rest[:cut]), rest[cut + 2 : -1], line, cfg, bare=False
                )
            # `at async /path/x.js:1:2` and `at new file:///x.js:1:2`: the keyword is not the path
            return _frame(None, _strip_call_prefixes(rest), line, cfg, bare=True)
        m = _VITEST_LINE.match(line)
        if m:
            rest = m.group("rest")
            head, _, loc = rest.rpartition(" ")
            frame = _frame(_clean_function(head) if head else None, loc, line, cfg, bare=True)
            # `❯ test/a.test.ts (3 tests | 1 failed)` is a file header, not a frame.
            return frame if frame is not None and frame.line is not None else None
        return None
    except (ValueError, IndexError, re.error):  # defensive: a parser must not raise on odd input
        return None


def parse_frames(lines: Iterable[str], config: ParsingConfig | None = None) -> list[StackFrame]:
    """Parse every line that is a stack frame, in order."""
    cfg = config or load_config()
    return [f for f in (parse_frame(line, cfg) for line in lines) if f is not None]
