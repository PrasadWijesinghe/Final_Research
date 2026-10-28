"""Cohen's kappa between two label lists, and a reader for the filled manual-review CSV."""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's kappa for two equal-length label sequences (1.0 = perfect, 0 = chance level)."""
    if len(a) != len(b):
        raise ValueError("label lists must have the same length")
    n = len(a)
    if n == 0:
        raise ValueError("no labels to compare")
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if expected == 1.0:  # both raters used one identical label only
        return 1.0
    return (observed - expected) / (1 - expected)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def kappa_from_files(filled: Path, key: Path, rater: str = "human_label") -> dict[str, Any]:
    """Kappa of `rater` (default the human column) against the rule label stored in `key`.

    Rows whose `rater` cell is blank are skipped and counted. Also returns the confusion pairs.
    """
    rule = {r["job_id"]: r["rule_label"] for r in _read(key)}
    a: list[str] = []
    b: list[str] = []
    skipped = 0
    for row in _read(filled):
        label = (row.get(rater) or "").strip().lower()
        if not label or row["job_id"] not in rule:
            skipped += 1
            continue
        a.append(label)
        b.append(rule[row["job_id"]])
    result: dict[str, Any] = {"n": len(a), "skipped_blank": skipped, "kappa": None}
    if a:
        result["kappa"] = cohen_kappa(a, b)
        result["agreement"] = sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
        result["confusion"] = dict(Counter(zip(a, b, strict=True)))
    return result
