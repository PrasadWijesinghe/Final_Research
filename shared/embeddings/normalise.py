"""Log normalisation: replace the noisy, run-specific parts of a log with placeholders."""

from __future__ import annotations

import re

# Order matters: ANSI first (it hides inside other tokens), timestamps before numbers/paths.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]"), ""),
    (re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b[\\])"), ""),
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<TS>",
    ),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TS>"),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<ID>",
    ),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    # 7+ hex chars containing at least one digit (commit shas, hashes); plain words stay.
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b", re.IGNORECASE), "<HEX>"),
    # Windows paths, then absolute unix paths (keep the file name's extension out of it).
    (re.compile(r"\b[A-Za-z]:[\\](?:[^\s\\:*?<>|]+[\\])*[^\s\\:*?<>|]*"), "<PATH>"),
    (re.compile(r"(?<![\w:/.-])/(?:[\w.@+-]+/)+[\w.@+-]*"), "<PATH>"),
    (re.compile(r"(?<![\w/.-])\.{1,2}/(?:[\w.@+-]+/)*[\w.@+-]+"), "<PATH>"),
]
_SPACES = re.compile(r"[ \t]+")


def normalise_log(text: str) -> str:
    """Strip ANSI, timestamps, hex ids and paths from a log, replacing them with placeholders."""
    for pattern, placeholder in _RULES:
        text = pattern.sub(placeholder, text)
    lines = (_SPACES.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)
