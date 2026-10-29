"""Text of a failure for the knowledge base: one builder used for cases AND for live queries.

Cases and queries must be written the same way, or a full-text match and an embedding match would
compare unlike things. `failure_part` turns parsed failing tests into a short block:

    error family: timeout (a timeout)
    test: Foo > bar | file: test/foo.test.ts
    error: TimeoutError: Exceeded timeout of <dur> | src/foo.ts:run

`signature` (from `parsing/normalise.py`) already replaces durations, ports, ids and paths with
placeholders and drops line numbers, so two runs of one failure give the same text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from testing_expert.parsing import FailedTest, signature
from testing_expert.triage.evidence import FAMILY_TEXT, failure_text

from .tags import error_families

# Strip placeholders' angle brackets for the full-text side: `<dur>` would otherwise be one word.
_PLACEHOLDER = re.compile(r"<(\w+)>")


def failure_part(failures: Sequence[FailedTest], tail: str, max_tests: int) -> tuple[str, str]:
    """(text block, primary error family) of the first `max_tests` failing tests."""
    chosen = list(failures[:max_tests])
    _, primary = error_families(failure_text(failures), tail)
    lines = [f"error family: {primary} ({FAMILY_TEXT.get(primary, 'unclassified error')})"]
    for f in chosen:
        where = f" | file: {f.file}" if f.file else ""
        lines.append(f"test: {f.full_name}{where}")
        sig = signature(f.error_type, f.message, f.frames)
        lines.append(f"error: {sig}")
    return "\n".join(lines), primary


def plain(text: str) -> str:
    """Placeholders as plain words (`<dur>` becomes `dur`), for the full-text index."""
    return _PLACEHOLDER.sub(r"\1", text)


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector text form of a vector."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
