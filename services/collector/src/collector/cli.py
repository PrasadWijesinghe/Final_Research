"""Command line entry point (`uv run collector ...`): backfill, fill-logs, daily, status, more."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from collector.backfill import (
    DEFAULT_DAYS,
    DEFAULT_WINDOW_DAYS,
    LOGS_ALL,
    LOGS_FAILED,
    Backfiller,
)
from collector.daily import DailySummary, DailySyncer, NotBackfilledError
from collector.export_import import TransferError, export_data, import_data, resolve_target
from collector.file_tags import load_file_tagger
from collector.repo_config import DEFAULT_CONFIG_DIR, MEMBERS, ConfigError, load_member_config
from collector.status import collect_status, fetch_rate_limit, render_status
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
    bf.add_argument(
        "--logs",
        choices=(LOGS_ALL, LOGS_FAILED),
        default=LOGS_ALL,
        help="'failed' skips logs of passing jobs (fetch them later with `fill-logs`); "
        "default 'all'",
    )
    fill = sub.add_parser(
        "fill-logs", help="fetch logs the policy wants but the database lacks, newest first"
    )
    fill.add_argument("--member", required=True, choices=MEMBERS)
    fill.add_argument("--repo", help="only this owner/name (must be in the member's list)")
    fill.add_argument("--limit", type=int, help="fetch at most this many logs in this run")
    fill.add_argument(
        "--data-dir", type=Path, help="where logs are written (default: DATA_DIR or ./data)"
    )
    daily = sub.add_parser(
        "daily", help="top up: new runs since the last sync plus a 14-day re-scan"
    )
    daily.add_argument("--member", required=True, choices=MEMBERS)
    daily.add_argument("--repo", help="only this owner/name (must be in the member's list)")
    daily.add_argument(
        "--data-dir", type=Path, help="where logs are written (default: DATA_DIR or ./data)"
    )
    status = sub.add_parser("status", help="show what is collected, gaps and the rate limit")
    status.add_argument("--member", required=True, choices=MEMBERS)
    exp = sub.add_parser("export", help="bundle the database, logs and a manifest into a tar.gz")
    exp.add_argument("--member", required=True, choices=MEMBERS)
    exp.add_argument("--database", help="database name (default: the one in DATABASE_URL)")
    exp.add_argument("--data-dir", type=Path, help="logs and snapshots folder (default: ./data)")
    exp.add_argument("--out-dir", type=Path, help="where the archive goes (default: data/exports)")
    imp = sub.add_parser("import", help="restore an export into the running stack and verify it")
    imp.add_argument("file", type=Path, help="the .tar.gz made by `collector export`")
    imp.add_argument("--force", action="store_true", help="overwrite a database that has rows")
    imp.add_argument("--database", help="database name (default: the one in DATABASE_URL)")
    imp.add_argument("--data-dir", type=Path, help="logs and snapshots folder (default: ./data)")
    return parser


def _missing_env(
    needed: Sequence[str] = ("GITHUB_TOKEN", "IDENTITY_SALT", "DATABASE_URL"),
) -> list[str]:
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
            logs=args.logs,
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


def run_fill_logs(args: argparse.Namespace) -> int:
    """Run `collector fill-logs` (pass 2 of the backfill); returns the process exit code."""
    missing = _missing_env(("GITHUB_TOKEN", "DATABASE_URL"))
    if missing:
        _err(
            f"{', '.join(missing)} not set. Put them in .env (see .env.example) and run with "
            "`uv run --env-file .env collector fill-logs ...`."
        )
        return EXIT_USAGE
    if args.limit is not None and args.limit < 1:
        _err("--limit must be at least 1")
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
    remaining: int | None = args.limit  # DEFAULT: --limit is a total over all repos of this run
    store = SqlStore(get_engine())
    with GitHubClient() as client:
        filler = Backfiller(
            client, store, member=args.member, data_dir=data_dir, tagger=tagger, rules=rules
        )
        for entry in repos:
            if remaining is not None and remaining <= 0:
                break
            try:
                summary = filler.fill_logs_repo(entry, remaining)
            except GitHubError as exc:
                failed += 1
                logger.error(
                    "fill-logs failed for repo; re-run to resume",
                    extra={"repo": entry.full_name, "error": str(exc), "status": exc.status_code},
                )
                _err(f"{entry.full_name}: failed ({exc}); re-run to resume")
                continue
            if remaining is not None:
                remaining -= summary.logs_ok + summary.logs_expired + summary.logs_missing
            sys.stdout.write(
                f"{entry.full_name}: logs ok {summary.logs_ok}, expired {summary.logs_expired}, "
                f"missing {summary.logs_missing}\n"
            )
    return EXIT_FAILED if failed else EXIT_OK


def _daily_line(full_name: str, s: DailySummary) -> str:
    window = f"{s.window_start:%Y-%m-%d %H:%M}..{s.window_end:%Y-%m-%d %H:%M} UTC"
    line = (
        f"{full_name}: window {window}; runs seen {s.runs_seen}, new or changed "
        f"{s.runs_processed}, already stored {s.runs_already_done}, attempts {s.attempts}, "
        f"jobs {s.jobs}, logs ok {s.logs_ok}"
    )
    if s.data_lost:
        line += (
            f"; WARNING: {s.gap_days} days since the last sync, runs older than 90 days are lost"
        )
    return line


def run_daily(args: argparse.Namespace) -> int:
    """Run `collector daily`; returns the process exit code."""
    missing = _missing_env()
    if missing:
        _err(
            f"{', '.join(missing)} not set. Put them in .env (see .env.example) and run with "
            "`uv run --env-file .env collector daily ...`."
        )
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
    store = SqlStore(get_engine())
    # Check every repo before any network call, so nothing is half done.
    with store.transaction():
        stored = {r.full_name: r for r in store.list_repos(args.member)}
        waiting = []
        for entry in repos:
            repo = stored.get(entry.full_name)
            state = store.get_sync_state(repo.id) if repo else None
            if state is None or state.last_daily_sync_at is None:
                waiting.append(entry.full_name)
    if waiting:
        _err(
            f"backfill has not finished for {', '.join(waiting)}. Run "
            f"`uv run --env-file .env collector backfill --member {args.member}` first."
        )
        return EXIT_USAGE
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)
    failed = 0
    with GitHubClient() as client:
        syncer = DailySyncer(
            client, store, member=args.member, data_dir=data_dir, tagger=tagger, rules=rules
        )
        for entry in repos:
            try:
                summary = syncer.daily_repo(entry)
            except NotBackfilledError as exc:
                _err(str(exc))
                return EXIT_USAGE
            except GitHubError as exc:
                failed += 1
                logger.error(
                    "repo daily sync failed; last_daily_sync_at not advanced",
                    extra={"repo": entry.full_name, "error": str(exc), "status": exc.status_code},
                )
                _err(f"{entry.full_name}: failed ({exc}); sync point not advanced, re-run later")
                continue
            sys.stdout.write(_daily_line(entry.full_name, summary) + "\n")
    return EXIT_FAILED if failed else EXIT_OK


def run_status(args: argparse.Namespace) -> int:
    """Run `collector status`; works without GITHUB_TOKEN (the rate limit is then unavailable)."""
    missing = _missing_env(("DATABASE_URL",))
    if missing:
        _err("DATABASE_URL not set. Put it in .env and run with `uv run --env-file .env ...`.")
        return EXIT_USAGE
    try:
        config = load_member_config(args.member)
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_USAGE
    now = datetime.now(UTC)
    report = collect_status(SqlStore(get_engine()), args.member, config.repos, now.date())
    if os.environ.get("GITHUB_TOKEN", "").strip():
        with GitHubClient() as client:
            rate = fetch_rate_limit(client)
    else:
        rate = fetch_rate_limit(None)
    sys.stdout.write(render_status(args.member, report, rate, now))
    return EXIT_OK


def run_export(args: argparse.Namespace) -> int:
    """Run `collector export`; returns the process exit code."""
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)
    out_dir = args.out_dir or data_dir / "exports"
    try:
        result = export_data(
            member=args.member,
            data_dir=data_dir,
            out_dir=out_dir,
            target=resolve_target(args.database),
        )
    except TransferError as exc:
        _err(f"export failed: {exc}")
        return EXIT_FAILED
    tables = sum(result.manifest["row_counts"].values())
    sys.stdout.write(
        f"wrote {result.archive} ({tables} rows in {len(result.manifest['row_counts'])} tables, "
        f"{len(result.manifest['files'])} files)\n"
    )
    return EXIT_OK


def run_import(args: argparse.Namespace) -> int:
    """Run `collector import`; returns the process exit code."""
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)
    try:
        result = import_data(
            args.file, data_dir=data_dir, target=resolve_target(args.database), force=args.force
        )
    except TransferError as exc:
        _err(f"import failed: {exc}")
        return EXIT_FAILED
    sys.stdout.write(
        f"imported {args.file}: {sum(result.row_counts.values())} rows in "
        f"{len(result.row_counts)} tables and {result.files} files; checksums and counts match\n"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the collector CLI."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "backfill":
        return run_backfill(args)
    if args.command == "fill-logs":
        return run_fill_logs(args)
    if args.command == "daily":
        return run_daily(args)
    if args.command == "status":
        return run_status(args)
    if args.command == "export":
        return run_export(args)
    if args.command == "import":
        return run_import(args)
    parser.print_help()
    return EXIT_OK
