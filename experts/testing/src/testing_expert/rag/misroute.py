"""Spots a failure that is not a test failure at all (P4.11), from patterns in the log.

Only used when the log shows no Jest, Mocha or Vitest failure. Each category (dependency, build,
infra) has weighted regexes in `config/test_diagnosis.yaml`; the best-scoring category wins when
its score reaches `min_score`. A log with a real test failure is never called misrouted here, even
if it also prints `npm ERR!` after the test report.
"""

from __future__ import annotations

from dataclasses import dataclass

from .settings import DiagnosisSettings

# DEFAULT: only the last 200,000 characters of a log are scanned (a bound for huge logs).
MAX_SCAN_CHARS = 200_000
# DEFAULT: at most 3 matching log lines are kept as evidence, each cut to 300 characters.
MAX_LINES = 3
LINE_CHARS = 300


@dataclass(frozen=True)
class Misroute:
    """The category the log points to, how strongly, and the log lines that matched."""

    category: str
    score: float
    lines: tuple[str, ...]


def detect(text: str, cfg: DiagnosisSettings) -> Misroute | None:
    """The best matching non-test category, or None when no category reaches the minimum score."""
    text = text[-MAX_SCAN_CHARS:]
    best: Misroute | None = None
    for category, patterns in cfg.misroute_patterns.items():
        score = 0.0
        lines: list[str] = []
        for pat in patterns:
            m = pat.regex.search(text)
            if m is None:
                continue
            score += pat.weight
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            line = text[start : end if end != -1 else len(text)].strip()[:LINE_CHARS]
            if line and line not in lines:
                lines.append(line)
        if score >= cfg.misroute_min_score and (best is None or score > best.score):
            best = Misroute(category, score, tuple(lines[:MAX_LINES]))
    return best
