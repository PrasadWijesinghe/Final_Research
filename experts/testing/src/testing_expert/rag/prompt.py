"""The diagnosis prompt: rendering, the reply schema and the prompt version (P4.11).

The prompt text lives in `prompts/diagnose.md`. Its top part is the system message, the part after
the `---USER---` line is the user message with `{{placeholders}}`. Everything that comes from a
log or from a past case is untrusted: it is cleaned, cut to a fixed size, run through the shared
secret sanitiser and wrapped in `<data>` tags, so no secret and no oversized text reaches the model.

`prompt_version` is the git object hash of the prompt file (what `git hash-object` prints), taken
as the first 12 hex characters, so it changes whenever the prompt text changes. It is computed
here in Python (SHA-1 over `blob <size>\\0` + the bytes), so it works in the Docker image, where
there is no `.git` and no git program, and gives the same value as git does.
"""

# ruff: noqa: E501
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from shared.sanitise import sanitise
from testing_expert.parsing import FailedTest
from testing_expert.triage.evidence import Overlap

from .settings import DiagnosisSettings

PROMPT_PATH = Path(__file__).parent / "prompts" / "diagnose.md"
_USER_MARKER = "---USER---"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_DATA_TAG = re.compile(r"</?\s*data\s*>", re.IGNORECASE)

CATEGORIES = ("dependency", "build", "infra", "other", "unknown")

# The JSON the model must return (sent to the gateway as `json_schema`).
REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "minLength": 1, "maxLength": 900},
        "remediation": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 400},
                "steps": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 6},
                "suggested_patch": {"type": "string", "maxLength": 4000},
            },
            "required": ["summary", "steps"],
            "additionalProperties": False,
        },
        "cited_case_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "misrouted": {"type": "boolean"},
        "suggested_category": {"enum": list(CATEGORIES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["root_cause", "remediation", "cited_case_ids", "misrouted", "confidence"],
    "additionalProperties": False,
}  # fmt: skip


@dataclass(frozen=True)
class CaseInfo:
    """One retrieved case as the prompt shows it."""

    case_id: str
    summary: str
    fix_summary: str
    tags: tuple[str, ...] = ()
    similarity: float = 1.0  # 0..1, from retrieval; the template only trusts close cases


@dataclass(frozen=True)
class PromptInput:
    """Everything the prompt shows. `verdict` is None when triage could not run."""

    job_name: str
    framework: str
    failures: Sequence[FailedTest]
    log_tail: str
    verdict: str | None
    confidence: float | None
    evidence: Sequence[str]
    overlap: Overlap | None
    cases: Sequence[CaseInfo]


@lru_cache(maxsize=1)
def prompt_version() -> str:
    """First 12 hex characters of the prompt file's git object hash (see the module doc)."""
    data = PROMPT_PATH.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()[:12]


@lru_cache(maxsize=1)
def _template() -> tuple[str, str]:
    system, _, user = PROMPT_PATH.read_text("utf-8").partition(_USER_MARKER)
    return system.strip(), user.strip()


def clean(text: str, limit: int) -> str:
    """Untrusted text made safe for the prompt: no control characters, no `<data>` tags, no
    secrets (shared sanitiser), cut to `limit` characters."""
    text = _DATA_TAG.sub("[tag removed]", _CONTROL.sub("", text))
    text = sanitise(text[: limit * 2]).text  # cut first: the sanitiser is line by line and slow
    return text[:limit]


def excerpt(inp: PromptInput, cfg: DiagnosisSettings) -> str:
    """The failing-test excerpt: names, files, error messages, diffs and project frames."""
    blocks: list[str] = []
    for f in inp.failures[: cfg.max_tests]:
        lines = [f"test: {f.full_name}" + (f" (file: {f.file})" if f.file else "")]
        if f.error_type or f.message:
            lines.append(
                f"error: {f.error_type + ': ' if f.error_type else ''}{f.message}"[
                    : cfg.max_message_chars
                ]
            )
        if f.diff:
            lines.append("diff:\n" + f.diff[: cfg.max_diff_chars])
        frames = [x for x in f.frames if x.kind == "project" and x.path][: cfg.max_frames]
        lines += [f"  at {x.function or '?'} ({x.path}:{x.line})" for x in frames]
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks) or inp.log_tail
    return clean(text, cfg.max_excerpt_chars)


def _overlap_text(o: Overlap | None) -> str:
    if o is None:
        return "not available"
    if not o.commit_files_known:
        return "The files changed by this commit are not known."
    parts = [f"{o.n_changed_files} file(s) changed by the commit."]
    parts.append(
        "The failing test file was changed."
        if o.test_file_changed
        else "The failing test file was not changed."
    )
    if o.stack_files_total:
        parts.append(
            f"{o.stack_files_changed or 0} of {o.stack_files_total} project file(s) in the stack "
            "trace were changed."
        )
    if o.first_hunk_frame is not None:
        p, line = o.first_hunk_frame
        parts.append(f"A stack frame ({clean(p, 200)}:{line}) is within 5 lines of changed code.")
    return " ".join(parts)


def _case_text(c: CaseInfo, cfg: DiagnosisSettings) -> str:
    tags = f"\ntags: {', '.join(c.tags)}" if c.tags else ""
    summary = clean(c.summary, cfg.case_text_chars)
    fix = clean(c.fix_summary, cfg.case_text_chars)
    return f"id: {c.case_id}{tags}\nfailure: {summary}\nfix: {fix}"


def render(inp: PromptInput, cfg: DiagnosisSettings) -> list[dict[str, str]]:
    """The chat messages (system, user) for the gateway."""
    system, user = _template()
    if inp.verdict is None:
        triage = "not available (no Jest, Mocha or Vitest failure could be read from the log)"
    else:
        triage = (
            f"verdict: {inp.verdict}, confidence {inp.confidence:.2f}"
            if inp.confidence is not None
            else f"verdict: {inp.verdict}"
        )
    evidence = "\n".join(f"- {clean(e, 300)}" for e in inp.evidence[: cfg.max_evidence_lines])
    cases = "\n\n".join(
        f"<data>\n{_case_text(c, cfg)}\n</data>" for c in inp.cases[: cfg.max_cases]
    )
    values = {
        "job_name": clean(inp.job_name, 100),
        "framework": inp.framework,
        "triage": triage,
        "evidence": evidence or "none",
        "overlap": _overlap_text(inp.overlap),
        "excerpt": excerpt(inp, cfg),
        "cases": cases or "none found",
    }
    # One pass, so a `{{word}}` inside an untrusted value is never filled in.
    user = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), user)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
