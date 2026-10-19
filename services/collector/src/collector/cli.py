"""Command line entry point (`uv run collector ...`): `backfill` now, daily and status later."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from collector.backfill import DEFAULT_DAYS, DEFAULT_WINDOW_DAYS, Backfiller
from collector.file_tags import load_file_tagger
from collector.repo_config import DEFAULT_CONFIG_DIR, MEMBERS, ConfigError, load_member_config
from collector.store import SqlStore
from collector.workflow_detection import load_detection_rules
from shared.db.session import get_engine
from shared.github import GitHubClient, GitHubError

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# DEFAULT: collected logs and snapshots live in `<checkout>/data` unless DATA_DIR says otherwise.
DEFAULT_DATA_DIR = DEFAULT_CONFIG_DIR.parent / "data"


def _err(message: str) -> None:
    sys.stderr.write(f"collector: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector", description="PipelineIQ data collector")
    sub = parser.add_subparsers(dest="command")
    bf = sub.add_parser("backfill", help="pull the last 90 days for a member's repos")
    bf.add_argument("--member", required=True, choices=MEMBERS)
    bf.add_argument("--repo", help="only this owner/name (must be in the member's list)")
    bf.add_argument("--days", type=int, default=DEFAULT_DAYS, help="how far back (default 90)")
    bf.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help=argparse.SUPPRESS)
    bf.add_argument(
        "--data-dir", type=Path, help="where logs are written (default: DATA_DIR or ./data)"
    )
    return parser


def _missing_env() -> list[str]:
    needed = ("GITHUB_TOKEN", "IDENTITY_SALT", "DATABASE_URL")
    return [name for name in needed if not os.environ.get(name, "").strip()]


def run_backfill(args: argparse.Namespace) -> int:
    """Run `collector backfill`; returns the process exit code."""
    missing = _missing_env()
    if missing:
        _err(
            f"{', '.join(missing)} not set. Put them in .env (see .env.example) and run with "
            "`uv run --env-file .env collector backfill ...`."
        )
        return EXIT_USAGE
    if args.days < 1 or args.window_days < 1:
        _err("--days and --window-days must be at least 1")
        return EXIT_USAGE
    try:
        config = load_member_config(args.member)
        tagger = load_file_tagger()
        rules = load_detection_rules()
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_USAGE
    repos = config.enabled_repos()
    if args.repo:
        repos = [r for r in config.repos if r.full_name == args.repo]
        if not repos:
            _err(f"{args.repo} is not in config/repos/{args.member}.yaml")
            return EXIT_USAGE
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)
    failed = 0
    with GitHubClient() as client:
        backfiller = Backfiller(
            client,
            SqlStore(get_engine()),
            member=args.member,
            data_dir=data_dir,
            tagger=tagger,
            rules=rules,
            days=args.days,
            window_days=args.window_days,
        )
        for entry in repos:
            try:
                summary = backfiller.backfill_repo(entry)
            except GitHubError as exc:
                failed += 1
                logger.error(
                    "repo backfill failed; re-run to resume",
                    extra={"repo": entry.full_name, "error": str(exc), "status": exc.status_code},
                )
                _err(f"{entry.full_name}: failed ({exc}); re-run to resume")
                continue
            sys.stdout.write(f"{entry.full_name}: {summary}\n")
    return EXIT_FAILED if failed else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the collector CLI."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "backfill":
        return run_backfill(args)
    parser.print_help()
    return EXIT_OK
