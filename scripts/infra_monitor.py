"""Watch one remote Git branch and submit real revisions to the trained Infra model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

from infra_pre_push import PIPELINE_ROOT, ZERO_SHA, _git, build_context, request_prediction

# DEFAULT: five-second polling; isolated bare caches contain objects but never execute repo code.
DEFAULT_INTERVAL = 5.0


class RemoteMonitor:
    """Fetch a branch into a dedicated bare repository, checkpointing only delivered predictions."""

    def __init__(self, repository: str, branch: str, cache_root: Path) -> None:
        subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{branch}"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        self.repository = repository
        self.branch = branch
        key = hashlib.sha256(f"{repository}\n{branch}".encode()).hexdigest()[:24]
        self.cache = cache_root / key
        self.cache.mkdir(parents=True, exist_ok=True)
        if not (self.cache / "HEAD").is_file():
            _git("init", "--bare", str(self.cache), cwd=cache_root)
        self.state_path = self.cache / "infra-state.json"
        self.previous = ZERO_SHA
        if self.state_path.exists():
            self.previous = json.loads(self.state_path.read_text("utf-8"))["head_sha"]
        self.first_poll = True

    def poll(self, api_url: str, timeout: float = 20.0) -> str | None:
        """Fetch the latest head; retry failed submissions and re-publish on monitor startup."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
        subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=",
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                self.repository,
                f"+refs/heads/{self.branch}:refs/heads/observed",
            ],
            cwd=self.cache,
            env=env,
            check=True,
            capture_output=True,
            timeout=60,
        )
        head = _git("rev-parse", "refs/heads/observed", cwd=self.cache)
        if head == self.previous and not self.first_poll:
            return None
        base = self.previous
        if self.first_poll and head == base:
            base = json.loads(self.state_path.read_text("utf-8")).get("base_sha", ZERO_SHA)
        context = build_context(
            cwd=self.cache,
            local_ref=f"refs/heads/{self.branch}",
            head_sha=head,
            remote_sha=base,
            remote_url=self.repository,
            event="push",
        )
        request_prediction(context, api_url, timeout)
        # Atomic replacement means an interrupted write does not destroy the previous checkpoint.
        pending = self.state_path.with_suffix(".tmp")
        pending.write_text(json.dumps({"head_sha": head, "base_sha": base}), encoding="utf-8")
        pending.replace(self.state_path)
        self.previous = head
        self.first_poll = False
        return head


def heartbeat(api_url: str, connection_id: str, state: str, error: str | None) -> None:
    """Report connectivity without logging Git stderr, which can contain credential details."""
    url = api_url.rsplit("/", 1)[0] + "/heartbeat"
    payload = {"connection_id": connection_id, "state": state, "error": error}
    request = Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=10):
        pass


def main() -> int:
    """Run continuously until stopped, or poll once for a command-line check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")  # DEFAULT: editable in the dashboard.
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--api-url", default="http://127.0.0.1:8104/monitor/predict")
    parser.add_argument("--cache-dir", type=Path, default=PIPELINE_ROOT / "data" / "infra-monitor")
    parser.add_argument("--connection-id", default="")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least one second")
    monitor = RemoteMonitor(args.repo, args.branch, args.cache_dir)
    while True:
        try:
            head = monitor.poll(args.api_url)
            if head:
                print(f"Infra prediction delivered for {head[:12]}", flush=True)
            if args.connection_id:
                heartbeat(args.api_url, args.connection_id, "connected", None)
        except Exception:
            # Error bodies may contain remote URLs or tokens; send only a fixed user-facing message.
            error = "Cannot fetch the branch or reach the model. Check repository access and API."
            print(error, flush=True)
            if args.connection_id:
                try:
                    heartbeat(args.api_url, args.connection_id, "error", error)
                except Exception:
                    pass
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
