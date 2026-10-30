"""`parse_log`: one call from a raw job log to a `ParsedLog`.

It cleans the log, asks the detector which frameworks printed output, runs each matching parser
and merges what they found. It never raises: whatever goes wrong comes back as
`parsed_ok=False` with a warning that names the exception type (never its text, which could
carry log content).

Confidence says how far to trust the result:
- 1.0: the runner printed a summary and the failures we found agree with it,
- 0.7: a summary exists but the counts disagree (a trimmed log, or a failure we missed),
- 0.4: failures were found but the runner printed no summary,
- 0.0: nothing recognisable (no tests ran, or a runner we do not parse).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from testing_expert.parsing import jest, mocha, nodetest, vitest
from testing_expert.parsing._common import (
    PartialParse,
    Run,
    dedupe_failures,
    dedupe_results,
    merge_summaries,
    prepare_lines,
)
from testing_expert.parsing.clean import CleanedLog, clean_bytes, clean_file, clean_text
from testing_expert.parsing.detect import Detection, detect
from testing_expert.parsing.models import (
    CaseResult,
    FailedTest,
    Framework,
    Granularity,
    ParsedLog,
    Summary,
)
from testing_expert.parsing.settings import ParsingConfig, load_config

log = logging.getLogger(__name__)

_PARSERS: dict[str, Callable[[Sequence[str], ParsingConfig | None], PartialParse]] = {
    "jest": jest.parse,
    "mocha": mocha.parse,
    "vitest": vitest.parse,
    "node-test": nodetest.parse,
}
_GRANULARITY_RANK: dict[Granularity, int] = {"none": 0, "file": 1, "test": 2}
# The confidence values are the plan's: 1.0 match, 0.7 mismatch, 0.4 failures without a summary.
CONFIDENCE_MATCH = 1.0
CONFIDENCE_MISMATCH = 0.7
CONFIDENCE_NO_SUMMARY = 0.4
CONFIDENCE_NONE = 0.0


def reports_failures(summaries: Sequence[Summary]) -> bool:
    """True when any summary says a test failed or a file failed."""
    return any((s.failed or 0) > 0 or (s.files_failed or 0) > 0 for s in summaries)


# DEFAULT: every run is checked against its own summary (so a retry that prints the same failure
# again agrees too). A run agrees when the failed tests found equal `failed`, the distinct failing
# files equal `files_failed` (jest `Test Suites:`, vitest `Test Files`), and a run that reports only
# failed files has at least one suite failure. Failed tests found after the last summary (a log cut
# off before it) are a disagreement.
def counts_agree(runs: Sequence[Run], stray: int = 0) -> bool:
    """Do the failures found in each run match the summary that run printed?"""
    if stray:
        return False
    for run in runs:
        s = run.summary
        if s.failed is not None and run.tests != s.failed:
            return False
        if s.files_failed is not None and run.files is not None and run.files != s.files_failed:
            return False
        if (s.failed or 0) == 0 and (s.files_failed or 0) > 0 and run.suites == 0:
            return False
    return True


def _score_and_run(
    cleaned: CleanedLog, hint: str | None, cfg: ParsingConfig
) -> tuple[Framework, list[tuple[str, PartialParse]], list[str], bool]:
    """Detect, then run the matching parsers. Returns (framework, parts, warnings, any_error)."""
    # Every parser regex assumes lines that are capped and right-stripped; do it once here.
    lines = prepare_lines(cleaned.lines, cfg.parse.max_line_chars)
    commands = [step.command for step in cleaned.steps] + [
        text for step in cleaned.steps for text in step.script
    ]
    detections: list[Detection] = detect(lines, commands, hint, cfg)
    runnable = [
        d for d in detections if d.framework in _PARSERS and d.score >= cfg.parse.detect_min_score
    ]
    eligible = [d for d in detections if d.score >= cfg.parse.detect_min_score]
    framework: Framework = eligible[0].framework if eligible else "other"
    parts: list[tuple[str, PartialParse]] = []
    warnings: list[str] = []
    error = False
    for det in runnable:
        try:
            parts.append((det.framework, _PARSERS[det.framework](lines, cfg)))
        except Exception as exc:  # noqa: BLE001 - a parser bug must not stop the pipeline
            error = True
            warnings.append(f"{det.framework} parser failed: {type(exc).__name__}")
            log.warning(
                "log parser failed", extra={"framework": det.framework, "error": type(exc).__name__}
            )
    if len(runnable) > 1:
        names = ", ".join(d.framework for d in runnable)
        warnings.append(f"several test runners printed output ({names}); results were merged")
    return framework, parts, warnings, error


def _merge(
    parts: list[tuple[str, PartialParse]], cfg: ParsingConfig
) -> tuple[list[FailedTest], list[CaseResult], list[Run], int, Granularity, list[str], bool]:
    failures: list[FailedTest] = []
    results: list[CaseResult] = []
    runs: list[Run] = []
    stray = 0
    warnings: list[str] = []
    truncated = False
    granularity: Granularity = "none"
    for _name, part in parts:
        truncated = truncated or part.truncated
        failures.extend(part.failures)
        results.extend(part.results)
        runs.extend(part.runs)
        stray += part.stray
        warnings.extend(part.warnings)
        if _GRANULARITY_RANK[part.granularity] > _GRANULARITY_RANK[granularity]:
            granularity = part.granularity
    unique, _cut = dedupe_failures(failures, cfg.parse.max_failures)
    merged_results, _results_cut = dedupe_results(results, cfg.parse.max_results)
    return unique, merged_results, runs, stray, granularity, warnings, truncated


def _config_warning(exc: Exception) -> str:
    """Say why the parsing config could not be loaded (the reason, never file content)."""
    if isinstance(exc, FileNotFoundError):
        return (
            "parsing config not found: put config/test_log_parsing.yaml next to the code or set "
            "TEST_LOG_PARSING_FILE (a container needs the file copied in)"
        )
    return f"parsing config is not usable: {type(exc).__name__}"


def parse_log(
    source: CleanedLog | bytes | str | Path,
    *,
    hint: str | None = None,
    config: ParsingConfig | None = None,
) -> ParsedLog:
    """Parse a job log into a `ParsedLog`. Never raises.

    `source` is a `CleanedLog`, raw log bytes (plain or gzip), log text, or a path to a log file.
    `hint` is the repo config's framework name, used only to break ties between detections.
    """
    try:
        try:
            cfg = config or load_config()
        except (OSError, ValueError, KeyError, TypeError) as exc:  # yaml errors are ValueErrors
            log.warning("parsing config not loaded", extra={"error": type(exc).__name__})
            return ParsedLog(
                framework="other", parsed_ok=False, confidence=0.0, warnings=(_config_warning(exc),)
            )
        if isinstance(source, CleanedLog):
            cleaned = source
        elif isinstance(source, Path):
            cleaned = clean_file(source, cfg)
        elif isinstance(source, bytes):
            cleaned = clean_bytes(source, cfg)
        else:
            cleaned = clean_text(source, cfg)
        framework, parts, warnings, error = _score_and_run(cleaned, hint, cfg)
        failures, results, runs, stray, granularity, part_warnings, cut = _merge(parts, cfg)
        truncated = cleaned.truncated or cut
        warnings = [*cleaned.warnings, *warnings, *part_warnings]
        summaries = [r.summary for r in runs]
        summary = merge_summaries(summaries)
        parsed_ok = not error
        if runs:
            agree = counts_agree(runs, stray)
            confidence = CONFIDENCE_MATCH if agree else CONFIDENCE_MISMATCH
            if not agree:
                found = sum(r.tests for r in runs) + stray
                said = sum(s.failed or 0 for s in summaries)
                warnings.append(
                    f"{found} failed test(s) found in the runs, their summaries report {said}"
                    + (" (the log was cut or a limit was reached)" if truncated else "")
                )
            if reports_failures(summaries) and not any(
                f.kind in ("test", "suite") for f in failures
            ):
                parsed_ok = False
                warnings.append("the summary reports failures but no failing test was found")
            if len(runs) > 1:
                warnings.append(f"{len(runs)} summaries found; counts were added together")
        else:
            confidence = CONFIDENCE_NO_SUMMARY if failures else CONFIDENCE_NONE
        return ParsedLog(
            framework=framework,
            results=tuple(results),
            failures=tuple(failures),
            summary=summary,
            granularity=granularity,
            parsed_ok=parsed_ok,
            confidence=confidence,
            warnings=tuple(warnings),
            truncated=truncated,
            summary_count=len(runs),
        )
    except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
        log.warning("parse_log failed", extra={"error": type(exc).__name__})
        return ParsedLog(
            framework="other",
            parsed_ok=False,
            confidence=0.0,
            warnings=(f"log could not be parsed: {type(exc).__name__}",),
        )
