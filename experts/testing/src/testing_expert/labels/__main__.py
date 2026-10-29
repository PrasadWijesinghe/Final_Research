"""Command line: label the member's test job attempts, export the review sample, compute kappa.

    uv run --env-file .env python -m testing_expert.labels --member test
    uv run python -m testing_expert.labels --kappa data/reports/test_labels_sample.csv \
        --key data/reports/test_labels_sample_key.csv
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import text

from .kappa import kappa_from_files
from .rules import run_rules
from .sample import DEFAULT_SEED, PER_STRATUM, export_sample


def _print_counts(conn, member: str) -> None:  # type: ignore[no-untyped-def]
    q = """
    select l.sub_label, l.confidence, l.rule_name, count(*) from testing.labels l
    join raw.jobs j on j.job_id = l.job_id join raw.workflow_runs w
      on w.run_id = j.run_id and w.attempt = j.attempt
    join raw.repos r on r.id = w.repo_id and r.member = :m
    where l.labelled_by = 'rule' group by 1, 2, 3 order by 1, 2, 3"""
    print("labels (label / confidence / rule: count)")
    for lab, conf, rule, n in conn.execute(text(q), {"m": member}):
        print(f"  {lab:9} {conf:5} {rule:26} {n}")
    q2 = """
    select r.full_name, l.sub_label, count(*) from testing.labels l
    join raw.jobs j on j.job_id = l.job_id join raw.workflow_runs w
      on w.run_id = j.run_id and w.attempt = j.attempt
    join raw.repos r on r.id = w.repo_id and r.member = :m
    where l.labelled_by = 'rule' group by 1, 2 order by 1, 2"""
    per: dict[str, dict[str, int]] = {}
    for repo, lab, n in conn.execute(text(q2), {"m": member}):
        per.setdefault(repo, {})[lab] = int(n)
    print("labels per repo")
    for repo, d in per.items():
        print(f"  {repo:38} " + " ".join(f"{k}={v}" for k, v in d.items()))


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.labels", description=__doc__)
    ap.add_argument("--member", help="collector member, e.g. test")
    ap.add_argument("--no-sample", action="store_true", help="skip the CSV export")
    ap.add_argument("--sample-only", action="store_true", help="export CSV, do not relabel")
    ap.add_argument("--out-dir", type=Path, default=Path("data/reports"))
    ap.add_argument("--per-stratum", type=int, default=PER_STRATUM)
    ap.add_argument("--seed", default=DEFAULT_SEED)
    ap.add_argument("--kappa", type=Path, help="filled sample CSV: print Cohen's kappa and exit")
    ap.add_argument("--key", type=Path, help="key CSV written with the sample (for --kappa)")
    ap.add_argument("--rater", default="human_label", help="column to score (or ai_prelabel)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.kappa:
        key = args.key or args.kappa.with_name(args.kappa.stem + "_key.csv")
        print(json.dumps(kappa_from_files(args.kappa, key, args.rater), indent=2, default=str))
        return 0
    if not args.member:
        ap.error("--member is required")

    from shared.db.session import get_engine

    engine = get_engine()
    if not args.sample_only:
        with engine.begin() as conn:
            cover = run_rules(conn, args.member)
        print(f"universe: {cover}")
    with engine.connect() as conn:
        _print_counts(conn, args.member)
        if not args.no_sample:
            sp, kp, n = export_sample(conn, args.member, args.out_dir, args.per_stratum, args.seed)
            print(f"sample: {n} rows -> {sp} (key: {kp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
