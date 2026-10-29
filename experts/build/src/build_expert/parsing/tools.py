"""Recognise compiler, linter, formatter and bundler output in a block of cleaned log lines.

Each parser looks for the error format its tool prints and returns a `Finding`: the tool, error
and warning counts, error codes (TS2322, ESLint rule ids, ...) and file locations. Formats were
checked against real output of TypeScript 5, ESLint 9, Prettier 3, Vite 6, webpack 5 and esbuild
(see tests/fixtures/logs/README.md).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Monorepo task runners prefix each line: pnpm -r "packages/web typecheck: ...",
# turbo "@acme/web:typecheck: ...". The prefix is removed before matching.
# pnpm's prefix starts with the package's folder (always has a "/", or is "." for the root).
_TASK_PREFIX = re.compile(r"^(?:(?:\.|[@\w.-]*/[@\w./-]+) [\w:.-]+: |[@\w./-]+:[\w:.-]+: )")
# Absolute checkout paths on GitHub-hosted runners, removed to get repo-relative paths.
_RUNNER_ROOT = re.compile(
    r"^(?:file://)?(?:/home/runner/work/[^/]+/[^/]+/|/Users/runner/work/[^/]+/[^/]+/|"
    r"[A-Za-z]:[\\/]a[\\/][^\\/]+[\\/][^\\/]+[\\/])"
)

# --- TypeScript compiler (tsc, vue-tsc) ------------------------------------------------------
_TSC_PLAIN = re.compile(
    r"^(?P<path>[^\s(:][^(]*?)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.*)$"
)
_TSC_PRETTY = re.compile(
    r"^(?P<path>[^\s:][^:]*?):(?P<line>\d+):(?P<col>\d+) - error (?P<code>TS\d+): (?P<msg>.*)$"
)
_TSC_GLOBAL = re.compile(r"^error (?P<code>TS\d+): (?P<msg>.*)$")
# Next.js runs the type checker during `next build` and prints its own prefix.
_NEXT_TYPE_ERROR = re.compile(r"^Type error: (?P<msg>.+)$")
# TS5xxx/TS6xxx option errors come from tsconfig.json itself (TS6133 "unused" is a code error).
_TSC_CONFIG_CODES = re.compile(r"^TS(?:5\d{3}|6046|6053|6059|6231|6304|6305|6306|6307|6310)$")

# --- ESLint (stylish formatter, the default) -------------------------------------------------
_ESLINT_FILE = re.compile(r"^(?P<path>(?:/|[A-Za-z]:\\|\.{1,2}/)?[^\s:*?\"<>|]+\.[A-Za-z0-9]+)$")
_ESLINT_PROBLEM = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<rest>.+?)\s*$"
)
_ESLINT_RULE_TAIL = re.compile(r"^(?P<msg>.*?)\s{2,}(?P<rule>[@\w][\w@/.-]*)$")
_ESLINT_SUMMARY = re.compile(
    r"^[✖×] (?P<total>\d+) problems? \((?P<errors>\d+) errors?, (?P<warnings>\d+) warnings?\)"
)
_ESLINT_MAX_WARNINGS = re.compile(r"ESLint found too many warnings \(maximum: (?P<max>\d+)\)")
_ESLINT_CRASH = re.compile(r"^Oops! Something went wrong! :\(")

# --- Prettier --check ------------------------------------------------------------------------
_PRETTIER_START = re.compile(r"^Checking formatting\.\.\.$")
_PRETTIER_SUMMARY = re.compile(
    r"^\[warn\] Code style issues (?:found in (?P<n>\d+) files|found in the above file|detected)"
)
_PRETTIER_FILE = re.compile(r"^\[warn\] (?P<path>\S.*)$")
_PRETTIER_ERROR = re.compile(
    r"^\[error\] (?P<path>[^:\s][^:]*): (?P<msg>.*?)(?: \((?P<line>\d+):(?P<col>\d+)\))?$"
)

# --- Bundlers --------------------------------------------------------------------------------
_VITE_FAILED = re.compile(r"^(?:error during build:|✗ Build failed in \S+)")
_VITE_PLUGIN = re.compile(r"\[(?:vite|plugin):?(?P<plugin>[\w:@/.-]*)\]")
_COULD_NOT_RESOLVE = re.compile(r'Could not resolve "(?P<spec>[^"]+)"(?: from "(?P<path>[^"]+)")?')
_ROLLUP_FAILED_RESOLVE = re.compile(
    r'Rollup failed to resolve import "(?P<spec>[^"]+)" from "(?P<path>[^"]+)"'
)
_ROLLUP_CLI = re.compile(r"^\[!\] (?:RollupError: |Error: )?(?P<msg>.+)$")
_WEBPACK_ERROR = re.compile(r"^ERROR in (?P<path>\S+)(?: (?P<line>\d+):(?P<col>\d+)\S*)?")
_WEBPACK_NOT_FOUND = re.compile(r"Module not found: (?:Error: )?Can't resolve '(?P<spec>[^']+)'")
_WEBPACK_SUMMARY = re.compile(r"^webpack(?: [\d.]+)? compiled with (?P<n>\d+) errors?")
_MODULE_BUILD_FAILED = re.compile(r"^Module build failed")
_ESBUILD_ERROR = re.compile(r"^(?:✘|X) \[ERROR\] (?P<msg>.+)$")
_ESBUILD_LOCATION = re.compile(r"^\s+(?P<path>[^\s:]+):(?P<line>\d+):(?P<col>\d+):$")
_NEXT_FAILED = re.compile(r"^Failed to compile\.?$")

# --- Hints that point away from a build failure ----------------------------------------------
_HINTS: dict[str, re.Pattern[str]] = {
    "dependency": re.compile(
        r"npm (?:ERR!|error) code (?:ERESOLVE|E404|ETARGET|EINTEGRITY|EBADENGINE)|"
        r"ERESOLVE (?:could not resolve|unable to resolve)|ERR_PNPM_[A-Z_]+|\bYN0\d{3}: |"
        r"Unsupported engine|engine \"node\" is incompatible"
    ),
    "resource": re.compile(
        r"JavaScript heap out of memory|Reached heap limit|No space left on device|\bENOSPC\b"
    ),
    "runner": re.compile(
        r"The runner has received a shutdown signal|lost communication with the server|"
        r"The operation was canceled"
    ),
    "network": re.compile(
        r"\b(?:ETIMEDOUT|ECONNRESET|ECONNREFUSED|EAI_AGAIN)\b|socket hang up|getaddrinfo ENOTFOUND"
    ),
    "test": re.compile(
        r"Test Files\s+\d+ failed|Tests:?\s+\d+ failed|^\s*\d+ failing$|^\s*FAIL\s+\S|"
        r"\bAssertionError\b"
    ),
}

SUB_LABEL_OF_TOOL = {"tsc": "tsc", "eslint": "eslint", "prettier": "format", "bundler": "bundler"}
# DEFAULT: keep at most this many locations per tool and step; counts still include every error.
# A lint run over a big repo can print thousands of warnings.
MAX_LOCATIONS = 200


@dataclass(frozen=True)
class Location:
    """One error or warning position. `path` is repo-relative when the runner prefix is known."""

    path: str | None
    line: int | None
    column: int | None
    code: str | None
    message: str
    severity: str = "error"


@dataclass
class Finding:
    """What one tool reported in a block of log lines."""

    tool: str  # tsc | eslint | prettier | bundler
    first_line: int  # index of the first matching line, to order tools within a step
    errors: int = 0
    warnings: int = 0
    codes: Counter[str] = field(default_factory=Counter)
    locations: list[Location] = field(default_factory=list)
    bundler: str | None = None  # vite | rollup | webpack | esbuild | next, for tool="bundler"
    config_error: bool = False  # the tool's own config failed (tsconfig option, ESLint crash)
    max_warnings_exceeded: bool = False

    @property
    def sub_label(self) -> str:
        """The label used in FailureLabel.sub_label for this tool."""
        return SUB_LABEL_OF_TOOL[self.tool]

    @property
    def failed(self) -> bool:
        """True when the output itself shows a failure (errors, or warnings over the limit)."""
        return self.errors > 0 or self.max_warnings_exceeded or self.config_error


def _keep(finding: Finding, location: Location) -> None:
    """Store a location unless the finding already holds MAX_LOCATIONS of them."""
    if len(finding.locations) < MAX_LOCATIONS:
        finding.locations.append(location)


def repo_relative(path: str) -> str:
    """Strip the runner checkout prefix and a leading './' from a reported path."""
    path = _RUNNER_ROOT.sub("", path.strip())
    return path.replace("\\", "/").removeprefix("./")


def strip_task_prefix(line: str) -> str:
    """Remove a pnpm -r / turbo per-package prefix from a log line."""
    return _TASK_PREFIX.sub("", line, count=1)


def _int(value: str | None) -> int | None:
    return int(value) if value else None


def _parse_tsc(lines: list[str]) -> Finding | None:
    finding: Finding | None = None
    for i, line in enumerate(lines):
        m = _TSC_PLAIN.match(line) or _TSC_PRETTY.match(line)
        if m:
            path: str | None = repo_relative(m.group("path"))
            code, msg = m.group("code"), m.group("msg")
            loc = Location(path, _int(m.group("line")), _int(m.group("col")), code, msg)
        else:
            g = _TSC_GLOBAL.match(line)
            n = _NEXT_TYPE_ERROR.match(line) if not g else None
            if not g and not n:
                continue
            code = g.group("code") if g else "next_type_error"
            msg = g.group("msg") if g else (n.group("msg") if n else "")
            loc = Location(None, None, None, code, msg)
        finding = finding or Finding(tool="tsc", first_line=i)
        finding.errors += 1
        finding.codes[code] += 1
        _keep(finding, loc)
        if _TSC_CONFIG_CODES.match(code) and loc.path is None:
            finding.config_error = True
        elif _TSC_CONFIG_CODES.match(code) and (loc.path or "").endswith(".json"):
            finding.config_error = True
    return finding


def _parse_eslint(lines: list[str]) -> Finding | None:
    finding: Finding | None = None
    current: str | None = None
    summary: tuple[int, int] | None = None
    for i, line in enumerate(lines):
        if _ESLINT_CRASH.match(line):
            finding = finding or Finding(tool="eslint", first_line=i)
            finding.config_error = True
            finding.codes["eslint_crash"] += 1
            continue
        s = _ESLINT_SUMMARY.match(line)
        if s:
            finding = finding or Finding(tool="eslint", first_line=i)
            summary = (
                (summary[0] if summary else 0) + int(s.group("errors")),
                (summary[1] if summary else 0) + int(s.group("warnings")),
            )
            continue
        if _ESLINT_MAX_WARNINGS.search(line):
            finding = finding or Finding(tool="eslint", first_line=i)
            finding.max_warnings_exceeded = True
            continue
        p = _ESLINT_PROBLEM.match(line)
        if p and current is not None:
            finding = finding or Finding(tool="eslint", first_line=i)
            rest = p.group("rest")
            tail = _ESLINT_RULE_TAIL.match(rest)
            rule = tail.group("rule") if tail else None
            msg = tail.group("msg") if tail else rest
            if rule is None and msg.startswith("Parsing error"):
                rule = "parsing-error"
            sev = p.group("sev")
            if sev == "error":
                finding.errors += 1
            else:
                finding.warnings += 1
            if rule:
                finding.codes[rule] += 1
            _keep(
                finding,
                Location(current, int(p.group("line")), int(p.group("col")), rule, msg, sev),
            )
            continue
        f = _ESLINT_FILE.match(line)
        if f and not line.startswith(" "):
            current = repo_relative(f.group("path"))
        elif line.strip() == "":
            continue
        elif not line.startswith(" "):
            current = None
    if finding is not None and summary is not None:
        # The summary line is authoritative (problem lines may be cut by log limits).
        finding.errors, finding.warnings = summary
    return finding


def _parse_prettier(lines: list[str]) -> Finding | None:
    started = any(_PRETTIER_START.match(x) or _PRETTIER_SUMMARY.match(x) for x in lines)
    if not started:
        return None
    finding: Finding | None = None
    for i, line in enumerate(lines):
        if _PRETTIER_SUMMARY.match(line):
            finding = finding or Finding(tool="prettier", first_line=i)
            continue
        e = _PRETTIER_ERROR.match(line)
        if e:
            finding = finding or Finding(tool="prettier", first_line=i)
            finding.errors += 1
            finding.codes["syntax_error"] += 1
            _keep(
                finding,
                Location(
                    repo_relative(e.group("path")),
                    _int(e.group("line")),
                    _int(e.group("col")),
                    "syntax_error",
                    e.group("msg"),
                ),
            )
            continue
        w = _PRETTIER_FILE.match(line)
        if w:
            finding = finding or Finding(tool="prettier", first_line=i)
            finding.errors += 1
            finding.codes["unformatted_file"] += 1
            path = repo_relative(w.group("path"))
            _keep(finding, Location(path, None, None, "unformatted_file", "not formatted"))
    return finding


def _bundler_finding(finding: Finding | None, i: int, bundler: str) -> Finding:
    if finding is None:
        return Finding(tool="bundler", first_line=i, bundler=bundler)
    if finding.bundler in (None, "rollup") and bundler != "rollup":
        finding.bundler = bundler  # Vite reports Rollup errors; prefer the outer tool's name
    return finding


def _parse_bundlers(lines: list[str]) -> Finding | None:
    finding: Finding | None = None
    summary_errors: int | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if _VITE_FAILED.match(line) or _ROLLUP_FAILED_RESOLVE.search(line):
            finding = _bundler_finding(finding, i, "vite")
            r = _ROLLUP_FAILED_RESOLVE.search(line)
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if r:
                _add_unresolved(finding, r.group("spec"), r.group("path"), line)
            elif line.startswith("error during build:") and nxt:
                c = _COULD_NOT_RESOLVE.search(nxt)
                if c:
                    _add_unresolved(finding, c.group("spec"), c.group("path"), nxt)
                    i += 1
                elif not finding.locations:
                    plugin = _VITE_PLUGIN.search(nxt)
                    code = f"plugin:{plugin.group('plugin')}" if plugin else "build_error"
                    finding.errors += 1
                    finding.codes[code] += 1
                    _keep(finding, Location(None, None, None, code, nxt))
        elif _WEBPACK_ERROR.match(line):
            m = _WEBPACK_ERROR.match(line)
            assert m is not None
            finding = _bundler_finding(finding, i, "webpack")
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            nf = _WEBPACK_NOT_FOUND.search(nxt)
            code = (
                "unresolved_import"
                if nf
                else ("loader_error" if _MODULE_BUILD_FAILED.match(nxt) else "build_error")
            )
            finding.errors += 1
            finding.codes[code] += 1
            _keep(
                finding,
                Location(
                    repo_relative(m.group("path")),
                    _int(m.group("line")),
                    _int(m.group("col")),
                    code,
                    nxt.strip() or line,
                ),
            )
        elif _WEBPACK_SUMMARY.match(line):
            m2 = _WEBPACK_SUMMARY.match(line)
            assert m2 is not None
            finding = _bundler_finding(finding, i, "webpack")
            summary_errors = int(m2.group("n"))
        elif _ESBUILD_ERROR.match(line):
            m3 = _ESBUILD_ERROR.match(line)
            assert m3 is not None
            finding = _bundler_finding(finding, i, "esbuild")
            msg = m3.group("msg")
            loc_path: str | None = None
            loc_line = loc_col = None
            for j in range(i + 1, min(i + 4, len(lines))):
                lm = _ESBUILD_LOCATION.match(lines[j])
                if lm:
                    loc_path = repo_relative(lm.group("path"))
                    loc_line, loc_col = int(lm.group("line")), int(lm.group("col"))
                    break
            code = "unresolved_import" if msg.startswith("Could not resolve") else "build_error"
            finding.errors += 1
            finding.codes[code] += 1
            _keep(finding, Location(loc_path, loc_line, loc_col, code, msg))
        elif _ROLLUP_CLI.match(line):
            m4 = _ROLLUP_CLI.match(line)
            assert m4 is not None
            finding = _bundler_finding(finding, i, "rollup")
            c = _COULD_NOT_RESOLVE.search(line)
            if c:
                _add_unresolved(finding, c.group("spec"), c.group("path"), line)
            else:
                finding.errors += 1
                finding.codes["build_error"] += 1
                _keep(finding, Location(None, None, None, "build_error", m4.group("msg")))
        elif _NEXT_FAILED.match(line):
            finding = _bundler_finding(finding, i, "next")
            if not finding.errors:
                finding.errors += 1
                finding.codes["build_error"] += 1
        i += 1
    if finding is not None and summary_errors is not None:
        finding.errors = max(finding.errors, summary_errors)
    return finding


def _add_unresolved(finding: Finding, spec: str, path: str | None, message: str) -> None:
    finding.errors += 1
    finding.codes["unresolved_import"] += 1
    _keep(
        finding,
        Location(repo_relative(path) if path else None, None, None, "unresolved_import", message),
    )


def parse_tools(lines: list[str]) -> list[Finding]:
    """All tools found in the lines, ordered by where each first appears."""
    plain = [strip_task_prefix(line) for line in lines]
    found = [
        f
        for f in (_parse_tsc(plain), _parse_eslint(plain), _parse_prettier(plain))
        if f is not None
    ]
    bundler = _parse_bundlers(plain)
    if bundler is not None:
        # Next.js type errors are reported by tsc; keep "Failed to compile" as the bundler only
        # when no type error explains it.
        if not (bundler.bundler == "next" and any(f.tool == "tsc" for f in found)):
            found.append(bundler)
    return sorted(found, key=lambda f: f.first_line)


def find_hints(lines: list[str]) -> set[str]:
    """Signatures of non-build causes present in the lines (dependency, resource, test, ...)."""
    hints: set[str] = set()
    for line in lines:
        for name, pattern in _HINTS.items():
            if name not in hints and pattern.search(line):
                hints.add(name)
    return hints
