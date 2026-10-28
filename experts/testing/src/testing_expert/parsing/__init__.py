"""Log parsing for the Test expert: `parse_log` turns a job log into failures, results and totals.

Smaller pieces are exported too: the cleaner, the stack-frame parser, the framework detector and
the normaliser used for retrieval.
"""

from testing_expert.parsing.clean import (
    CleanedLog,
    Step,
    clean_bytes,
    clean_file,
    clean_line,
    clean_stream,
    clean_text,
)
from testing_expert.parsing.detect import Detection, detect
from testing_expert.parsing.models import (
    CaseResult,
    FailedTest,
    FailureKind,
    FileSource,
    FrameKind,
    Framework,
    Granularity,
    Outcome,
    ParsedLog,
    StackFrame,
    Summary,
)
from testing_expert.parsing.normalise import normalise, signature
from testing_expert.parsing.parse import parse_log
from testing_expert.parsing.settings import ParsingConfig, load_config
from testing_expert.parsing.stack import parse_frame, parse_frames

__all__ = [
    "CaseResult",
    "CleanedLog",
    "Detection",
    "FailedTest",
    "FailureKind",
    "FileSource",
    "FrameKind",
    "Framework",
    "Granularity",
    "Outcome",
    "ParsedLog",
    "ParsingConfig",
    "StackFrame",
    "Step",
    "Summary",
    "clean_bytes",
    "clean_file",
    "clean_line",
    "clean_stream",
    "clean_text",
    "detect",
    "load_config",
    "normalise",
    "parse_frame",
    "parse_frames",
    "parse_log",
    "signature",
]
