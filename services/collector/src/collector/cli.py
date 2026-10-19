"""Command line entry point (`uv run collector ...`). P1.5 adds backfill, daily and status."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run the collector CLI; for now it only parses arguments (subcommands arrive in P1.5)."""
    parser = argparse.ArgumentParser(prog="collector", description="PipelineIQ data collector")
    parser.parse_args(argv)
    return 0
