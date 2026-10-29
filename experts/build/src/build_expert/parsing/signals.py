"""Per-job summary of a log: which steps failed, which tools reported what, and warning counts."""

from __future__ import annotations

from dataclasses import dataclass, field

from .clean import split_sections
from .tools import Finding, find_hints, parse_tools

# DEFAULT: bump when parsing changes in a way that changes labels; datasets record it.
PARSER_VERSION = "1"


@dataclass
class StepSignals:
    """One step of the log with what the parsers found in it."""

    index: int
    title: str
    exit_code: int | None
    findings: list[Finding] = field(default_factory=list)
    hints: set[str] = field(default_factory=set)

    @property
    def failed(self) -> bool:
        """True when GitHub logged a non-zero exit code for this step."""
        return self.exit_code is not None and self.exit_code != 0


@dataclass
class JobSignals:
    """Everything the build parsers learned from one job log."""

    steps: list[StepSignals]

    @property
    def failed_steps(self) -> list[StepSignals]:
        """Steps that ended with a non-zero exit code, in order."""
        return [s for s in self.steps if s.failed]

    def _sum(self, tool: str, attr: str) -> int:
        return sum(int(getattr(f, attr)) for s in self.steps for f in s.findings if f.tool == tool)

    @property
    def lint_errors(self) -> int:
        """ESLint errors across the whole job."""
        return self._sum("eslint", "errors")

    @property
    def lint_warnings(self) -> int:
        """ESLint warnings across the whole job (also counted when the job passed)."""
        return self._sum("eslint", "warnings")

    @property
    def type_errors(self) -> int:
        """TypeScript errors across the whole job."""
        return self._sum("tsc", "errors")

    @property
    def unformatted_files(self) -> int:
        """Files Prettier reported as not formatted."""
        return self._sum("prettier", "errors")

    @property
    def bundle_errors(self) -> int:
        """Bundler errors across the whole job."""
        return self._sum("bundler", "errors")

    @property
    def tools_seen(self) -> set[str]:
        """Every tool that printed recognisable output, passing or failing."""
        return {f.tool for s in self.steps for f in s.findings}


def parse_job_log(text: str) -> JobSignals:
    """Split a raw job log into steps and run every parser on each step."""
    steps: list[StepSignals] = []
    for section in split_sections(text):
        steps.append(
            StepSignals(
                index=section.index,
                title=section.title,
                exit_code=section.exit_code,
                findings=parse_tools(section.lines),
                hints=find_hints(section.lines),
            )
        )
    return JobSignals(steps=steps)
