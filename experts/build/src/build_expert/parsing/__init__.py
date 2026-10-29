"""Log parsing for build and code-quality failures: clean a job log, split it into steps, and
find compiler, linter, formatter and bundler errors in each step.

Used by labelling (which failures are build failures) and by features (warning counts of earlier
runs). The same code serves training and, later, diagnosis.
"""

from .clean import LogSection, clean_line, failed_sections, split_sections
from .signals import JobSignals, parse_job_log
from .tools import Finding, Location, find_hints, parse_tools

__all__ = [
    "Finding",
    "JobSignals",
    "Location",
    "LogSection",
    "clean_line",
    "failed_sections",
    "find_hints",
    "parse_job_log",
    "parse_tools",
    "split_sections",
]
