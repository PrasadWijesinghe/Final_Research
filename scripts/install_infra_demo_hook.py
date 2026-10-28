"""Install the advisory PipelineIQ pre-push hook into a local Git project."""

from __future__ import annotations

import argparse
import shlex
import stat
import subprocess
import sys
from pathlib import Path

MARKER = "# PipelineIQ Infra demo hook"


def _git_dir(project: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    path = Path(completed.stdout.strip())
    return path if path.is_absolute() else (project / path).resolve()


def install(project: Path, api_url: str, *, force: bool = False) -> Path:
    """Install or refresh this project's generated pre-push hook."""
    pipeline_root = Path(__file__).resolve().parents[1]
    python = (
        pipeline_root
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    if not python.is_file():
        raise FileNotFoundError(
            f"PipelineIQ environment not found: {python}; run uv sync --all-packages"
        )
    # Respect core.hooksPath and linked worktrees, rather than assuming .git/hooks.
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks/pre-push"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    hook_path = Path(result.stdout.strip())
    hook = hook_path if hook_path.is_absolute() else project / hook_path
    if hook.exists() and MARKER not in hook.read_text(encoding="utf-8") and not force:
        raise FileExistsError(f"{hook} already exists; use --force only after reviewing it")
    client = pipeline_root / "scripts" / "infra_pre_push.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                MARKER,
                f"exec {shlex.quote(python.as_posix())} {shlex.quote(client.as_posix())} "
                f'--api-url {shlex.quote(api_url)} "$@"',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "project", type=Path, help="Local Git working tree that should call PipelineIQ"
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8104/monitor/predict")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    hook = install(args.project.resolve(), args.api_url, force=args.force)
    print(f"Installed advisory PipelineIQ hook: {hook}")


if __name__ == "__main__":
    main()
