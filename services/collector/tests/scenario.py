"""Shared helpers: build a backfiller against the fake GitHub, and check the expected end state."""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from collector.backfill import Backfiller
from collector.daily import DailySyncer
from collector.file_tags import load_file_tagger
from collector.repo_config import RepoEntry
from collector.store import Store
from collector.workflow_detection import load_detection_rules
from recorded_github import (
    COMMIT_SECRET,
    FAKE_TOKEN,
    HEAD_SHA,
    LOG_SECRET,
    NOW,
    REPO,
    REPO_ID,
    SHA_A,
    SHA_B,
    SHA_C,
)

from shared.github import GitHubClient, GitHubConfig
from shared.sanitise import hash_identity

ENTRY = RepoEntry(full_name=REPO, licence="MIT")
Dump = dict[str, list[dict[str, Any]]]


def make_client() -> GitHubClient:
    """A client with a fake token, no retries and no real sleeping."""
    return GitHubClient(token=FAKE_TOKEN, config=GitHubConfig(max_retries=0), sleep=lambda _s: None)


def make_backfiller(
    store: Store, data_dir: Path, client: GitHubClient | None = None, logs: str = "all"
) -> Backfiller:
    """A Backfiller for the `test` member with a fixed clock (2026-10-19 12:00 UTC)."""
    return Backfiller(
        client or make_client(),
        store,
        member="test",
        data_dir=data_dir,
        tagger=load_file_tagger(),
        rules=load_detection_rules(),
        logs=logs,
        clock=lambda: NOW,
    )


def make_daily(
    store: Store,
    data_dir: Path,
    now: datetime,
    client: GitHubClient | None = None,
) -> DailySyncer:
    """A DailySyncer for the `test` member whose clock says `now`."""
    return DailySyncer(
        client or make_client(),
        store,
        member="test",
        data_dir=data_dir,
        tagger=load_file_tagger(),
        rules=load_detection_rules(),
        clock=lambda: now,
    )


def _by(rows: list[dict[str, Any]], *keys: str) -> dict[Any, dict[str, Any]]:
    return {(r[keys[0]] if len(keys) == 1 else tuple(r[k] for k in keys)): r for r in rows}


def read_log(data_dir: Path, storage_path: str) -> str:
    """Decompress a stored log."""
    return gzip.decompress((data_dir / storage_path).read_bytes()).decode("utf-8")


def check_expected_state(dump: Dump, data_dir: Path) -> None:
    """Assert the table contents and files produced by a full backfill of the fixture repo."""
    # repos and sync state
    assert [r["id"] for r in dump["repos"]] == [REPO_ID]
    repo = dump["repos"][0]
    assert (repo["full_name"], repo["member"], repo["licence"]) == (REPO, "test", "MIT")
    assert repo["default_branch"] == "main"
    state = dump["sync_state"][0]
    assert state["backfill_cursor"] is None
    assert state["backfill_done_at"] == NOW
    assert state["last_daily_sync_at"] == NOW

    # workflows: detection result stored once, at the default branch head
    wfs = _by(dump["workflows"], "path")
    assert wfs[".github/workflows/test.yml"]["relevant"] is True
    assert wfs[".github/workflows/test.yml"]["detected_at_sha"] == HEAD_SHA
    assert wfs[".github/workflows/release.yml"]["relevant"] is False

    # runs: one row per attempt; the release run is not collected
    runs = _by(dump["workflow_runs"], "run_id", "attempt")
    assert set(runs) == {(9001, 1), (9001, 2), (9002, 1), (9004, 1)}
    assert runs[(9001, 1)]["conclusion"] == "failure"
    assert runs[(9001, 2)]["conclusion"] == "success"
    assert runs[(9002, 1)]["pr_numbers"] == [57]
    assert runs[(9002, 1)]["actor_hash"] == hash_identity("dana-dev")
    assert runs[(9001, 2)]["actor_hash"] == hash_identity("lee-ci")
    assert runs[(9001, 1)]["workflow_path"] == ".github/workflows/test.yml"
    for run in runs.values():
        assert run["source"] == "collector"
        # logins and emails only ever appear hashed
        flat = repr(run)
        for personal in ("dana-dev", "lee-ci", "example.com", "avatar"):
            assert personal not in flat

    # jobs, with names mapped to workflow job keys
    jobs = _by(dump["jobs"], "job_id")
    assert set(jobs) == {5001, 5002, 5003, 5004, 5005, 5006, 5007, 5008}
    assert (jobs[5001]["run_id"], jobs[5001]["attempt"]) == (9001, 1)
    assert (jobs[5003]["run_id"], jobs[5003]["attempt"]) == (9001, 2)
    assert jobs[5001]["job_key"] == "test"
    assert jobs[5007]["job_key"] == "lint-and-more"
    assert jobs[5004]["runner_os"] == "Windows"
    assert jobs[5007]["runner_os"] == "macOS"
    assert jobs[5001]["runner_os"] == "Linux"
    assert "steps" not in jobs[5001]["raw"]
    assert len(dump["steps"]) == 8 * 4

    # commits and diffs
    commits = _by(dump["commits"], "sha")
    assert set(commits) == {SHA_A, SHA_B, SHA_C}
    assert commits[SHA_A]["author_hash"] == hash_identity("lee-ci")
    assert commits[SHA_B]["author_hash"] == hash_identity("dana-dev")
    assert commits[SHA_C]["author_hash"] == hash_identity("dana@example.com")  # no GitHub account
    assert commits[SHA_B]["parent_shas"] == [SHA_A]
    assert COMMIT_SECRET not in commits[SHA_B]["message"]
    assert "[REDACTED:" in commits[SHA_B]["message"]
    files = _by(dump["commit_files"], "sha", "path")
    big = files[(SHA_B, "src/big.js")]
    assert big["patch"] is None and big["patch_truncated"] is True
    assert big["additions"] == 9000
    small = files[(SHA_A, "src/a.js")]
    assert small["patch"].startswith("@@ -1,4 +1,6 @@") and small["patch_truncated"] is False
    assert files[(SHA_B, "lib/new.js")]["previous_path"] == "lib/old.js"
    assert files[(SHA_B, "lib/new.js")]["patch"] is None
    assert files[(SHA_B, "lib/new.js")]["patch_truncated"] is False
    assert len([f for f in files if f[0] == SHA_B]) == 5

    # config snapshots: workflow + changed tagged files; lockfile by reference
    snaps = _by(dump["file_snapshots"], "sha", "path")
    wf = ".github/workflows/test.yml"
    assert set(snaps) == {
        (SHA_A, wf),
        (SHA_A, "package.json"),
        (SHA_B, wf),
        (SHA_B, "package-lock.json"),
        (SHA_C, wf),
    }
    assert "npm test" in snaps[(SHA_A, wf)]["content"]
    assert '"jest"' in snaps[(SHA_A, "package.json")]["content"]
    lock = snaps[(SHA_B, "package-lock.json")]
    assert lock["content"] is None and lock["storage_path"].startswith("snapshots/blobs/")
    assert gzip.decompress((data_dir / lock["storage_path"]).read_bytes()).startswith(b"{")

    # logs: failed jobs and passing jobs of the relevant workflow; expired and missing as status
    logs = _by(dump["logs"], "job_id")
    assert set(logs) == {5001, 5002, 5003, 5004, 5005, 5007, 5008}  # 5006 was cancelled
    assert {j: logs[j]["status"] for j in logs} == {
        5001: "ok", 5002: "ok", 5003: "missing", 5004: "expired",
        5005: "ok", 5007: "ok", 5008: "ok",
    }  # fmt: skip
    for job_id in (5003, 5004):
        assert logs[job_id]["storage_path"] is None and logs[job_id]["sha256"] is None
    failed = logs[5001]
    assert failed["storage_path"] == "logs/acme/widgets/9001/1/5001.log.gz"
    assert logs[5003]["storage_path"] is None
    assert logs[5008]["storage_path"] == "logs/acme/widgets/9004/1/5008.log.gz"
    text = read_log(data_dir, failed["storage_path"])
    assert LOG_SECRET not in text
    assert "[REDACTED:" in text and "FAIL test/a.test.js" in text
    assert failed["redactions"] >= 1
    stored = (data_dir / failed["storage_path"]).read_bytes()
    assert failed["sha256"] == hashlib.sha256(stored).hexdigest()
    assert failed["bytes"] == len(stored)
    assert logs[5002]["redactions"] == 0

    # files on disk: exactly the logs and the lockfile blob, nothing else (no temp files)
    on_disk = sorted(p.relative_to(data_dir).as_posix() for p in data_dir.rglob("*") if p.is_file())
    expected = sorted(
        [logs[j]["storage_path"] for j in logs if logs[j]["storage_path"]] + [lock["storage_path"]]
    )
    assert on_disk == expected
