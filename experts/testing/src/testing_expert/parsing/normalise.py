"""Normalises error text so two failures that differ only in noise look the same.

`normalise()` swaps volatile values for placeholders (`<uuid>`, `<ts>`, `<dur>`, `<port>`,
`<addr>`, `<tmp>`, `<hex>`), using the ordered rules in config/test_log_parsing.yaml.
`signature()` builds the short fingerprint the knowledge base (P4.10) matches on: the error type,
the first message line and the top project frames, with no line numbers.
"""

from __future__ import annotations

from collections.abc import Sequence

from testing_expert.parsing.models import StackFrame
from testing_expert.parsing.settings import ParsingConfig, load_config


def normalise(text: str, config: ParsingConfig | None = None) -> str:
    """Replace timestamps, durations, ports, addresses, temp paths, UUIDs and long hex strings."""
    cfg = config or load_config()
    for rule in cfg.normalise.rules:
        text = rule.pattern.sub(rule.replace, text)
    return text


def signature(
    error_type: str | None,
    message: str | None,
    frames: Sequence[StackFrame],
    config: ParsingConfig | None = None,
) -> str:
    """Build a stable fingerprint: error type, first message line, top project frames.

    Frames contribute `path:function` (no line or column, which change with every edit); only
    `project` frames count, so Node internals and dependencies do not dilute the match.
    """
    cfg = config or load_config()
    first_line = ""
    for candidate in (message or "").splitlines():
        if candidate.strip():
            first_line = candidate.strip()
            break
    first_line = normalise(first_line, cfg)[: cfg.normalise.signature_message_chars]
    parts = [f"{error_type or 'Error'}: {first_line}".rstrip(": ").rstrip()]
    count = 0
    for frame in frames:
        if frame.kind != "project" or not frame.path:
            continue
        parts.append(f"{frame.path}:{frame.function}" if frame.function else frame.path)
        count += 1
        if count >= cfg.normalise.signature_frames:
            break
    return " | ".join(parts)
