"""CLI: python -m testing_expert.model train --dataset v1"""

from __future__ import annotations

import argparse
import logging

from .data import VARIANTS
from .train import pick_winner, run_training


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    p = argparse.ArgumentParser(prog="testing_expert.model")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train", help="baselines + models + calibration + export")
    t.add_argument("--dataset", default="v1")
    t.add_argument("--variants", nargs="+", default=list(VARIANTS), choices=VARIANTS)
    t.add_argument("--no-loro", action="store_true")
    t.add_argument("--no-mlflow", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    results = run_training(
        args.dataset,
        variants=tuple(args.variants),
        loro=not args.no_loro,
        use_mlflow=not args.no_mlflow,
    )
    if "all" in results:
        logging.getLogger(__name__).info("winner", extra={"winner": pick_winner(results)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
