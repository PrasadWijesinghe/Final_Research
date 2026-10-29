"""Clean a GitHub Actions job log and split it into one section per step.

A raw job log has a timestamp at the start of every line, ANSI colour codes, and GitHub's own
markers. Each `run:` or `uses:` step starts with `##[group]Run <command>`; the lines up to
`##[endgroup]` repeat the command and its settings, and the step's output follows. A failing step
ends with `##[error]Process completed with exit code N.`
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
# CSI sequences (colours, cursor moves) and the few two-byte escapes tools print.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_STEP_START = re.compile(r"^##\[group\]Run (?P<title>.*)$")
_EXIT = re.compile(r"^##\[error\]Process completed with exit code (?P<code>-?\d+)\.?$")
_POST = re.compile(r"^(?:Post job cleanup\.|##\[group\]Run Post |Cleaning up orphan processes)")


@dataclass
class LogSection:
    """The output of one step, cleaned. `index` 0 is the preamble before the first step."""

    index: int
    title: str  # e.g. "pnpm run typecheck" or "actions/checkout@v4"; "" for the preamble
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None  # set when GitHub logged "Process completed with exit code N"

    @property
    def failed(self) -> bool:
        """True when GitHub marked this step as failed with a non-zero exit code."""
        return self.exit_code is not None and self.exit_code != 0

    @property
    def is_action(self) -> bool:
        """True for `uses:` steps (owner/name@ref), False for shell `run:` steps."""
        return bool(re.match(r"^[\w.-]+/[\w./-]+@\S+$", self.title))

    def text(self) -> str:
        """The section's output as one string."""
        return "\n".join(self.lines)


def clean_line(line: str) -> str:
    """Drop the timestamp prefix, ANSI codes and trailing whitespace from one log line."""
    return _ANSI.sub("", _TIMESTAMP.sub("", line, count=1)).rstrip()


def split_sections(log_text: str) -> list[LogSection]:
    """Split a job log into the preamble plus one section per step, in order."""
    sections = [LogSection(index=0, title="")]
    in_header = False
    for raw in log_text.splitlines():
        line = clean_line(raw)
        start = _STEP_START.match(line)
        if start:
            sections.append(LogSection(index=len(sections), title=start.group("title").strip()))
            in_header = True
            continue
        current = sections[-1]
        if in_header:
            # Lines between "##[group]Run" and "##[endgroup]" repeat the step's settings.
            if line.startswith("##[endgroup]"):
                in_header = False
            continue
        exit_match = _EXIT.match(line)
        if exit_match:
            current.exit_code = int(exit_match.group("code"))
            continue
        if _POST.match(line):
            # Everything after the steps is clean-up; keep it out of the last step.
            sections.append(LogSection(index=len(sections), title="post"))
            continue
        if line.startswith(("##[group]", "##[endgroup]")):
            continue
        current.lines.append(line.removeprefix("##[error]").removeprefix("##[warning]"))
    return sections


def failed_sections(sections: list[LogSection]) -> list[LogSection]:
    """Sections GitHub marked as failed, in order (usually exactly one)."""
    return [s for s in sections if s.failed]
