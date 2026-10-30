"""`build-label-check`: the manual label check (PLAN B4b, docs/experts/build.md section 3).

1. `sample` picks failed jobs from a dataset's `job_decisions.parquet`, a few per labelling rule,
   and writes a blind sheet (no rule answers) plus a key file with the rules' answers.
2. Two people fill in the sheet separately: is it a build failure (yes/no), and which kind.
3. `score` compares the rules with each person, and the two people with each other (Cohen's kappa).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from sklearn.metrics import cohen_kappa_score

REPO_ROOT = Path(__file__).resolve().parents[5]
SHEET_COLUMNS = (
    "id",
    "repo",
    "url",
    "job_name",
    "failed_step",
    "error_excerpt",
    "is_build_failure",
    "kind",
    "note",
)
KEY_COLUMNS = ("id", "job_id", "claimed", "rule_name", "sub_label", "confidence")
KINDS = ("tsc", "eslint", "format", "bundler", "build_other")
INSTRUCTIONS = (
    "Open each url, read the failed step's log, and fill in: is_build_failure = yes or no "
    "(yes when the job failed because of compiling, type-checking, linting, formatting or "
    "bundling); kind = tsc, eslint, format, bundler or build_other (only when yes). "
    "Work alone: do not look at the key file or at the other person's sheet."
)


def _yes(value: str | None) -> int | None:
    v = (value or "").strip().lower()
    if v in ("yes", "y", "1", "true"):
        return 1
    if v in ("no", "n", "0", "false"):
        return 0
    return None


def sample(
    decisions: list[dict[str, Any]], per_rule: int, not_build: int, seed: int
) -> list[dict[str, Any]]:
    """Up to `per_rule` claimed first-attempt jobs per rule, plus `not_build` unclaimed ones
    spread over the rules that declined them."""
    rng = np.random.default_rng(seed)
    first = [d for d in decisions if d["attempt"] == 1]
    picked: list[dict[str, Any]] = []
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for d in first:
        if d["claimed"]:
            by_rule.setdefault(d["rule_name"], []).append(d)
    for rule in sorted(by_rule):
        jobs = by_rule[rule]
        take = rng.choice(len(jobs), size=min(per_rule, len(jobs)), replace=False)
        picked += [jobs[i] for i in sorted(take)]
    declined: dict[str, list[dict[str, Any]]] = {}
    for d in first:
        if not d["claimed"]:
            declined.setdefault(d["rule_name"], []).append(d)
    rules = sorted(declined)
    for i, rule in enumerate(rules):
        share = not_build // len(rules) + (1 if i < not_build % len(rules) else 0)
        jobs = declined[rule]
        take = rng.choice(len(jobs), size=min(share, len(jobs)), replace=False)
        picked += [jobs[j] for j in sorted(take)]
    order = rng.permutation(len(picked))  # shuffle so the sheet does not group by rule
    return [picked[i] for i in order]


def write_sheets(picked: list[dict[str, Any]], out_dir: Path) -> tuple[Path, Path]:
    """The blind sheet and the key file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet, key = out_dir / "label_check_sheet.csv", out_dir / "label_check_key.csv"
    # utf-8-sig so Excel opens it with the right characters.
    with sheet.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(SHEET_COLUMNS)
        for i, d in enumerate(picked, 1):
            w.writerow(
                [
                    i,
                    d["repo"],
                    d["url"],
                    d["job_name"],
                    d["failed_step"],
                    d["error_excerpt"] or "",
                    "",
                    "",
                    "",
                ]
            )
    with key.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(KEY_COLUMNS)
        for i, d in enumerate(picked, 1):
            w.writerow(
                [
                    i,
                    d["job_id"],
                    "yes" if d["claimed"] else "no",
                    d["rule_name"],
                    d["sub_label"] or "",
                    d["confidence"] or "",
                ]
            )
    return sheet, key


def _read(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {row["id"]: row for row in csv.DictReader(fh)}


def _kappa(a: list[Any], b: list[Any]) -> float | None:
    if len(a) < 2 or len(set(a) | set(b)) < 2:
        return None
    return round(float(cohen_kappa_score(a, b)), 3)


def score(key_path: Path, sheet_paths: Sequence[Path]) -> str:
    """A Markdown summary: rules against each checker, and checker against checker."""
    key = _read(key_path)
    sheets = [(_read(p), p.stem) for p in sheet_paths]
    lines = ["# Label check", ""]
    for sheet, name in sheets:
        ids = [i for i in key if i in sheet and _yes(sheet[i].get("is_build_failure")) is not None]
        if not ids:
            lines += [f"`{name}`: no answers yet.", ""]
            continue
        rule = [_yes(key[i]["claimed"]) or 0 for i in ids]
        human = [_yes(sheet[i]["is_build_failure"]) or 0 for i in ids]
        tp = sum(1 for r, h in zip(rule, human, strict=True) if r and h)
        fp = sum(1 for r, h in zip(rule, human, strict=True) if r and not h)
        fn = sum(1 for r, h in zip(rule, human, strict=True) if not r and h)
        agree = sum(1 for r, h in zip(rule, human, strict=True) if r == h) / len(ids)
        lines += [
            f"## Rules against `{name}` ({len(ids)} jobs answered)",
            "",
            f"- Agreement: {agree:.1%}; Cohen's kappa: {_kappa(rule, human)}",
            f"- Precision of the rules' build claims: {tp / (tp + fp):.1%}"
            if tp + fp
            else "- Precision of the rules' build claims: –",
            f"- Recall of real build failures: {tp / (tp + fn):.1%}"
            if tp + fn
            else "- Recall of real build failures: –",
            "",
            "| Rule | Jobs | Person agrees |",
            "|---|---|---|",
        ]
        per_rule: dict[str, list[int]] = {}
        for i, r, h in zip(ids, rule, human, strict=True):
            per_rule.setdefault(key[i]["rule_name"], []).append(int(r == h))
        for rule_name, hits in sorted(per_rule.items()):
            lines.append(f"| {rule_name} | {len(hits)} | {sum(hits) / len(hits):.0%} |")
        both = [
            i
            for i, r, h in zip(ids, rule, human, strict=True)
            if r and h and sheet[i].get("kind", "").strip()
        ]
        if both:
            same = sum(1 for i in both if sheet[i]["kind"].strip().lower() == key[i]["sub_label"])
            lines += ["", f"Kind (sub-label) agreement where both say build: {same}/{len(both)}"]
        lines.append("")
    if len(sheets) >= 2:
        (a, name_a), (b, name_b) = sheets[0], sheets[1]
        ids = [
            i
            for i in key
            if _yes(a.get(i, {}).get("is_build_failure")) is not None
            and _yes(b.get(i, {}).get("is_build_failure")) is not None
        ]
        ya = [_yes(a[i]["is_build_failure"]) for i in ids]
        yb = [_yes(b[i]["is_build_failure"]) for i in ids]
        lines += [
            f"## `{name_a}` against `{name_b}` ({len(ids)} jobs both answered)",
            "",
            f"- Build or not: Cohen's kappa {_kappa(ya, yb)}",
        ]
        kinds = [i for i in ids if a[i].get("kind", "").strip() and b[i].get("kind", "").strip()]
        if kinds:
            ka = [a[i]["kind"].strip().lower() for i in kinds]
            kb = [b[i]["kind"].strip().lower() for i in kinds]
            lines.append(f"- Kind: Cohen's kappa {_kappa(ka, kb)} over {len(kinds)} jobs")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Command-line options."""
    p = argparse.ArgumentParser(prog="build-label-check", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample", help="write a blind sheet and a key from a dataset")
    s.add_argument("--dataset", required=True, help="dataset version (v1) or a folder path")
    s.add_argument("--per-rule", type=int, default=8, help="claimed jobs per rule (default 8)")
    s.add_argument("--not-build", type=int, default=30, help="declined jobs in total (default 30)")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--out", type=Path, help="folder (default: the dataset folder)")
    s.add_argument("--data-dir", type=Path, help="data folder (default: DATA_DIR or ./data)")
    c = sub.add_parser("score", help="compare the rules and the people")
    c.add_argument("--key", type=Path, required=True)
    c.add_argument("--sheet", type=Path, action="append", required=True, help="one per person")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `build-label-check`."""
    args = build_parser().parse_args(argv)
    if args.command == "score":
        sys.stdout.write(score(args.key, args.sheet))
        return 0
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or REPO_ROOT / "data")
    folder = Path(args.dataset)
    if not folder.is_dir():
        folder = data_dir / "datasets" / "build" / args.dataset
    path = folder / "job_decisions.parquet"
    if not path.exists():
        sys.stderr.write(f"build-label-check: {path} not found (rebuild the dataset)\n")
        return 2
    picked = sample(pq.read_table(path).to_pylist(), args.per_rule, args.not_build, args.seed)
    sheet, key = write_sheets(picked, args.out or folder)
    sys.stdout.write(f"{len(picked)} jobs\n{sheet}\n{key}\n\n{INSTRUCTIONS}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
