"""CLI: `python -m testing_expert.kb build | search | eval` (P4.10).

build   mine cases, tag them, store them in testing.kb_cases and embed them. Safe to run again:
        the same case keeps its id, unchanged text keeps its embedding, and a stopped
        embedding step continues where it stopped (`--embed-only` skips mining).
search  top 5 cases for a text, for a demo (`--as-of` and `--permissive-only` are the filters).
eval    the retrieval sanity check; writes a JSON file with the numbers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import psycopg

from .evaluate import evaluate
from .mine import mine
from .retrieve import Query, search
from .store import embed_pending, pending_count, upsert_cases


def _conn() -> psycopg.Connection:  # type: ignore[type-arg]
    return psycopg.connect(os.environ["DATABASE_URL"].replace("+psycopg", ""))


def cmd_build(args: argparse.Namespace) -> int:
    """Mine, store and embed."""
    t0 = time.perf_counter()
    with _conn() as conn:
        if not args.embed_only:
            rows, skipped = mine(conn, limit=args.limit, workers=args.workers)
            n = upsert_cases(conn, rows)
            print(f"mined and stored {n} cases in {time.perf_counter() - t0:.0f}s")
            print("skipped:", json.dumps(dict(skipped)))
        if args.mine_only:
            return 0
        print(f"embedding {pending_count(conn)} cases ...")
        res = embed_pending(
            conn, progress=lambda d, t: print(f"  embedded {d}/{t}", flush=True), allow_fake=False
        )
        print("embedding:", json.dumps(res))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Print the top cases for a text."""
    cutoff = datetime.fromisoformat(args.as_of) if args.as_of else None
    q = Query(" ".join(args.text), cutoff=cutoff, run_id=args.run_id, head_sha=args.sha)
    with _conn() as conn:
        hits = search(conn, q, k=args.k, mode=args.mode, permissive_only=args.permissive_only)
    for i, h in enumerate(hits, start=1):
        print(
            f"{i}. [{h.kind}] {h.repo}  score={h.score:.4f} sim={h.similarity:.2f} "
            f"(fts#{h.fts_rank} vec#{h.vector_rank})"
        )
        print(f"   {h.failure_summary}")
        print(f"   fix: {h.fix_summary[:300]}")
        print(f"   tags: {' '.join(h.tags)}")
        print(f"   {h.source_url}")
    if not hits:
        print("no cases found")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Run the evaluation and save the numbers."""
    with _conn() as conn:
        res = evaluate(conn, sample=args.sample, seed=args.seed)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["table"], indent=1))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.kb", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="mine, tag and embed")
    b.add_argument("--limit", type=int, default=None, help="candidates per rule (trial runs)")
    b.add_argument("--workers", type=int, default=6)
    b.add_argument("--embed-only", action="store_true")
    b.add_argument("--mine-only", action="store_true")
    b.set_defaults(fn=cmd_build)
    s = sub.add_parser("search", help="top cases for a text")
    s.add_argument("text", nargs="+")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--mode", choices=["hybrid", "fts", "vector"], default="hybrid")
    s.add_argument("--as-of", help="only cases verified at or before this ISO time")
    s.add_argument("--run-id", type=int, help="leave out cases from this run")
    s.add_argument("--sha", help="leave out cases from this commit")
    s.add_argument("--permissive-only", action="store_true")
    s.set_defaults(fn=cmd_search)
    e = sub.add_parser("eval", help="retrieval sanity check")
    e.add_argument("--sample", type=int, default=300)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--out", default="experts/testing/reports/kb_v1_metrics.json")
    e.set_defaults(fn=cmd_eval)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
