"""Backfill: pull the last N days of runs, jobs, commits, config files and logs for a repo.

Flow per repo (docs/DATA.md section 3.3): upsert the repo, sync its workflows and decide which are
relevant, then walk date windows from newest to oldest. Every run is processed in one database
transaction, and the window cursor is saved after each window, so an interrupted backfill can be
resumed and a second run changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collector.file_tags import FileTagger
from collector.job_keys import map_job_key
from collector.policy import FAILED_CONCLUSIONS, date_windows, gzip_bytes, should_fetch_log
from collector.repo_config import RepoEntry
from collector.store import (
    CommitFileRecord,
    CommitRecord,
    JobRecord,
    LogRecord,
    RepoRecord,
    RunRecord,
    SkippedRunRecord,
    SnapshotRecord,
    StepRecord,
    Store,
    SyncStateRecord,
    WorkflowRecord,
)
from collector.workflow_detection import DetectionRules, detect_relevant_workflow
from shared.github import GitHubClient, GitHubError, GitHubNotFoundError
from shared.sanitise import SanitiseResult, hash_identity, sanitise

logger = logging.getLogger(__name__)

# DEFAULT: 7-day windows; the client halves any window that hits the 1,000-result cap.
DEFAULT_WINDOW_DAYS = 7
DEFAULT_DAYS = 90

# `--logs all` fetches every log the policy asks for; `--logs failed` leaves logs of passing jobs
# for a later `fill-logs` pass (collection order only; the end state is the same, D11 and D18).
LOGS_ALL = "all"
LOGS_FAILED = "failed"

# DEFAULT: personal data and bulky objects are dropped from the stored `raw` run JSON: logins and
# emails must only appear hashed (DATA 8), and the repository blobs repeat on every run.
_RAW_RUN_DROP = (
    "actor",
    "triggering_actor",
    "head_commit",
    "repository",
    "head_repository",
)

_NUL = "\x00"


def _clean(text: str | None) -> str | None:
    """Postgres text and jsonb cannot hold NUL characters."""
    return None if text is None else text.replace(_NUL, "")


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _int_list(values: Any) -> list[int]:
    return [int(v["number"]) for v in values or [] if isinstance(v, dict) and "number" in v]


def workflow_path_of(run: dict[str, Any]) -> str:
    """The workflow file path of a run (GitHub may append `@refs/...`)."""
    return str(run.get("path") or "").split("@", 1)[0]


def runner_os(labels: list[str]) -> str | None:
    """Guess the runner OS from job labels, else None."""
    # DEFAULT: derived from the first label naming an OS (ubuntu/linux, windows, macos).
    for label in labels:
        low = label.lower()
        if "ubuntu" in low or "linux" in low:
            return "Linux"
        if "windows" in low:
            return "Windows"
        if "macos" in low or "mac-os" in low:
            return "macOS"
    return None


@dataclass
class RepoSummary:
    """Counters for one repo, for the console and for tests."""

    runs_seen: int = 0
    runs_skipped_workflow: int = 0
    runs_already_done: int = 0
    runs_processed: int = 0
    attempts: int = 0
    jobs: int = 0
    commits: int = 0
    snapshots: int = 0
    logs_ok: int = 0
    logs_expired: int = 0
    logs_missing: int = 0
    logs_deferred: int = 0  # passing-job logs left for `fill-logs` (--logs failed)
    logs_failed: int = 0  # logs GitHub answered 5xx for; left pending for a later `fill-logs`
    runs_skipped_failing: int = 0  # runs skipped this time after persistent 5xx (kept for retry)
    skipped_cleared: int = 0  # previously skipped runs that were collected now
    windows_done: int = 0
    windows_total: int = 0


class Backfiller:
    """Runs the backfill for repos of one member against a GitHub client and a Store."""

    def __init__(
        self,
        client: GitHubClient,
        store: Store,
        *,
        member: str,
        data_dir: Path,
        tagger: FileTagger,
        rules: DetectionRules,
        days: int = DEFAULT_DAYS,
        window_days: int = DEFAULT_WINDOW_DAYS,
        logs: str = LOGS_ALL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        hash_fn: Callable[[str], str] = hash_identity,
        sanitise_fn: Callable[[str], SanitiseResult] = sanitise,
    ) -> None:
        self.client = client
        self.store = store
        self.member = member
        self.data_dir = data_dir
        self.tagger = tagger
        self.rules = rules
        self.days = days
        self.window_days = window_days
        if logs not in (LOGS_ALL, LOGS_FAILED):
            raise ValueError(f"logs must be '{LOGS_ALL}' or '{LOGS_FAILED}', not {logs!r}")
        self.logs = logs
        self._clock = clock
        self._hash = hash_fn
        self._sanitise = sanitise_fn
        # Per repo call: ids stored as skipped, and ids that failed during this call (not re-tried).
        self._skipped: set[int] = set()
        self._failed_runs: set[int] = set()

    # ---- per repo ---------------------------------------------------------------------------

    def backfill_repo(self, entry: RepoEntry) -> RepoSummary:
        """Backfill one repo; safe to call again after a crash or after completion."""
        fn = entry.full_name
        summary = RepoSummary()
        repo_id, default_branch, state = self._register_repo(entry)
        started_at, done = self._resume_point(state.backfill_cursor if state else None)
        relevant = self._sync_workflows(entry, repo_id, default_branch)
        self._retry_skipped(entry, repo_id, relevant, summary)

        windows = date_windows(started_at, self.days, self.window_days)
        summary.windows_total = len(windows)
        for index, (start, end) in enumerate(windows):
            if index < done:
                summary.windows_done += 1
                continue
            for run in self.client.iter_workflow_runs(fn, start, end):
                self._process_run(entry, repo_id, run, relevant, summary)
            with self.store.transaction():
                self.store.set_cursor(repo_id, self._cursor(started_at, index + 1))
            summary.windows_done += 1
            logger.info(
                "window done",
                extra={"repo": fn, "window": index + 1, "of": len(windows), "end": end.isoformat()},
            )
        with self.store.transaction():
            self.store.finish_backfill(repo_id, self._clock(), started_at)
        return summary

    def _register_repo(self, entry: RepoEntry) -> tuple[int, str | None, SyncStateRecord | None]:
        """Upsert the repo and its sync_state row; return (repo id, default branch, sync state)."""
        info = self.client.get_json(f"/repos/{entry.full_name}")
        repo_id = int(info["id"])
        default_branch = info.get("default_branch")
        with self.store.transaction():
            self.store.upsert_repo(
                RepoRecord(
                    id=repo_id,
                    full_name=entry.full_name,
                    member=self.member,
                    # DEFAULT: the licence recorded is the one in the member's config file;
                    # scripts/rerun_check.py (P1.3) flags a mismatch with GitHub's spdx_id.
                    licence=entry.licence,
                    default_branch=default_branch,
                    enabled=entry.enabled,
                )
            )
            self.store.ensure_sync_state(repo_id)
            state = self.store.get_sync_state(repo_id)
        return repo_id, default_branch, state

    def _cursor(self, started_at: datetime, windows_done: int) -> str:
        return json.dumps(
            {
                "started_at": started_at.isoformat(),
                "days": self.days,
                "window_days": self.window_days,
                "windows_done": windows_done,
            },
            sort_keys=True,
        )

    def _resume_point(self, cursor: str | None) -> tuple[datetime, int]:
        """Return (start time, windows done) from a saved cursor, or a fresh start."""
        if cursor:
            try:
                data = json.loads(cursor)
                if data["days"] == self.days and data["window_days"] == self.window_days:
                    return datetime.fromisoformat(data["started_at"]), int(data["windows_done"])
            except (ValueError, KeyError, TypeError):
                pass
            logger.warning("ignoring a cursor that does not match this backfill; starting over")
        return self._clock().replace(microsecond=0), 0

    # ---- workflows --------------------------------------------------------------------------

    def _sync_workflows(
        self, entry: RepoEntry, repo_id: int, default_branch: str | None
    ) -> dict[str, bool]:
        """Store each workflow file with its relevance flag; return {path: relevant}."""
        fn = entry.full_name
        explicit = set(entry.workflows)
        listed: dict[str, str | None] = {
            str(wf["path"]): wf.get("name")
            for wf in self.client.paginate(f"/repos/{fn}/actions/workflows", items_key="workflows")
        }
        for path in sorted(explicit):
            listed.setdefault(path, None)
        with self.store.transaction():
            existing = self.store.get_workflows(repo_id)
        head_sha: str | None = None
        relevant: dict[str, bool] = {}
        for path in sorted(listed):
            name = listed[path]
            known = existing.get(path)
            if explicit:
                record = WorkflowRecord(repo_id, path, name, path in explicit, None)
            elif known is not None and known.detected_at_sha is not None:
                relevant[path] = known.relevant
                continue
            else:
                if head_sha is None and default_branch:
                    branch = self.client.get_json(f"/repos/{fn}/branches/{default_branch}")
                    head_sha = str(branch["commit"]["sha"])
                record = self._detect(fn, repo_id, path, name, head_sha)
            relevant[path] = record.relevant
            if record != known:
                with self.store.transaction():
                    self.store.upsert_workflow(record)
        return relevant

    def _detect(
        self, fn: str, repo_id: int, path: str, name: str | None, sha: str | None
    ) -> WorkflowRecord:
        content = self.client.fetch_raw_file(fn, sha, path) if sha else None
        if content is None:
            # DEFAULT: a workflow file we cannot read is treated as not relevant and re-checked
            # on the next backfill (detected_at_sha stays empty).
            logger.warning("workflow file not readable; marked not relevant", extra={"repo": fn})
            return WorkflowRecord(repo_id, path, name, False, None)
        result = detect_relevant_workflow(
            content.decode("utf-8", errors="replace"), self.member, self.rules
        )
        return WorkflowRecord(repo_id, path, name, result.relevant, sha)

    # ---- one run ----------------------------------------------------------------------------

    def _process_run(
        self,
        entry: RepoEntry,
        repo_id: int,
        run: dict[str, Any],
        relevant: dict[str, bool],
        summary: RepoSummary,
    ) -> None:
        summary.runs_seen += 1
        path = workflow_path_of(run)
        run_id = int(run["id"])
        if not relevant.get(path, False):
            summary.runs_skipped_workflow += 1
            self._forget_skipped(repo_id, run_id, summary)
            return
        if run_id in self._failed_runs:
            return  # already failed in this call (the retry pass); do not spend retries again
        before = replace(summary)
        try:
            self._collect_run(entry, repo_id, run, path, summary)
        except GitHubError as exc:
            if not exc.is_server_error:
                raise  # network, auth and rate-limit failures stop the repo; a re-run resumes
            for f in fields(summary):  # the run was rolled back: its counters must not count
                setattr(summary, f.name, getattr(before, f.name))
            self._skip_run(entry, repo_id, run, exc, summary)

    def _forget_skipped(self, repo_id: int, run_id: int, summary: RepoSummary) -> None:
        """Remove a run from the skipped set (inside the caller's transaction or its own)."""
        if run_id in self._skipped:
            with self.store.transaction():
                self.store.clear_skipped_run(repo_id, run_id)
            self._skipped.discard(run_id)
            summary.skipped_cleared += 1

    def _skip_run(
        self,
        entry: RepoEntry,
        repo_id: int,
        run: dict[str, Any],
        exc: GitHubError,
        summary: RepoSummary,
    ) -> None:
        """Record a run GitHub keeps failing on, so the repo can finish; it is retried later."""
        run_id = int(run["id"])
        attempt = int(run.get("run_attempt") or 1)
        logger.warning(
            "run skipped after persistent GitHub server errors; will be retried on later runs",
            extra={
                "repo": entry.full_name,
                "run_id": run_id,
                "attempt": attempt,
                "status": exc.status_code,
                "error": str(exc),
            },
        )
        with self.store.transaction():
            self.store.add_skipped_run(
                SkippedRunRecord(repo_id, run_id, attempt, str(exc)[:200], self._clock())
            )
        self._skipped.add(run_id)
        self._failed_runs.add(run_id)
        summary.runs_skipped_failing += 1

    def _retry_skipped(
        self, entry: RepoEntry, repo_id: int, relevant: dict[str, bool], summary: RepoSummary
    ) -> None:
        """Try every previously skipped run of the repo again, before the normal walk."""
        fn = entry.full_name
        self._failed_runs = set()
        with self.store.transaction():
            records = self.store.skipped_runs(repo_id)
        self._skipped = {r.run_id for r in records}
        for record in records:
            try:
                run = self.client.get_json(f"/repos/{fn}/actions/runs/{record.run_id}")
            except GitHubNotFoundError:
                logger.warning(
                    "skipped run no longer exists; dropped from the skipped set",
                    extra={"repo": fn, "run_id": record.run_id},
                )
                self._forget_skipped(repo_id, record.run_id, summary)
                continue
            except GitHubError as exc:
                if not exc.is_server_error:
                    raise
                self._skip_run(
                    entry,
                    repo_id,
                    {"id": record.run_id, "run_attempt": record.attempt},
                    exc,
                    summary,
                )
                continue
            logger.info("retrying skipped run", extra={"repo": fn, "run_id": record.run_id})
            self._process_run(entry, repo_id, run, relevant, summary)

    def _collect_run(
        self,
        entry: RepoEntry,
        repo_id: int,
        run: dict[str, Any],
        path: str,
        summary: RepoSummary,
    ) -> None:
        """Collect one run in one transaction; any exception rolls the whole run back."""
        fn = entry.full_name
        run_id = int(run["id"])
        latest = int(run.get("run_attempt") or 1)
        with self.store.transaction():
            if self.store.attempt_done(repo_id, run_id, latest):
                summary.runs_already_done += 1
                self._forget_skipped(repo_id, run_id, summary)
                return
            attempts: dict[int, dict[str, Any]] = {latest: run}
            for k in range(1, latest):
                try:
                    attempts[k] = self.client.get_json(
                        f"/repos/{fn}/actions/runs/{run_id}/attempts/{k}"
                    )
                except GitHubNotFoundError:
                    logger.warning(
                        "attempt metadata not found; attempt skipped",
                        extra={"repo": fn, "run_id": run_id, "attempt": k},
                    )
            jobs = self.client.list_shrinking(
                f"/repos/{fn}/actions/runs/{run_id}/jobs", {"filter": "all"}, items_key="jobs"
            )
            by_attempt: dict[int, list[dict[str, Any]]] = {}
            for job in jobs:
                by_attempt.setdefault(int(job.get("run_attempt") or latest), []).append(job)
            for k in sorted(attempts):
                self._process_attempt(
                    entry, repo_id, k, attempts[k], by_attempt.get(k, []), path, summary
                )
            self._forget_skipped(repo_id, run_id, summary)
            summary.runs_processed += 1

    def _process_attempt(
        self,
        entry: RepoEntry,
        repo_id: int,
        attempt: int,
        meta: dict[str, Any],
        jobs: list[dict[str, Any]],
        workflow_path: str,
        summary: RepoSummary,
    ) -> None:
        fn = entry.full_name
        sha = str(meta["head_sha"])
        self._ensure_commit(fn, repo_id, sha, summary)
        self._ensure_snapshots(fn, repo_id, sha, workflow_path, summary)
        actor = (meta.get("actor") or {}).get("login")
        self.store.upsert_run(
            RunRecord(
                run_id=int(meta["id"]),
                attempt=attempt,
                repo_id=repo_id,
                workflow_path=workflow_path or None,
                head_sha=sha,
                head_branch=meta.get("head_branch"),
                event=meta.get("event"),
                status=meta.get("status"),
                conclusion=meta.get("conclusion"),
                created_at=_dt(meta.get("created_at")),
                run_started_at=_dt(meta.get("run_started_at")),
                updated_at=_dt(meta.get("updated_at")),
                actor_hash=self._hash(actor) if actor else None,
                pr_numbers=_int_list(meta.get("pull_requests")),
                raw=self._scrub_run(meta),
            )
        )
        summary.attempts += 1
        snapshot = self.store.get_snapshot(repo_id, sha, workflow_path)
        yaml_text = snapshot.content if snapshot else None
        for job in jobs:
            self._process_job(entry, repo_id, int(meta["id"]), attempt, job, yaml_text, summary)

    def _scrub_run(self, run: dict[str, Any]) -> dict[str, Any]:
        raw = {k: v for k, v in run.items() if k not in _RAW_RUN_DROP}
        title = raw.get("display_title")
        if isinstance(title, str):
            raw["display_title"] = _clean(self._sanitise(title).text)
        return raw

    # ---- jobs, steps, logs ------------------------------------------------------------------

    def _process_job(
        self,
        entry: RepoEntry,
        repo_id: int,
        run_id: int,
        attempt: int,
        job: dict[str, Any],
        workflow_yaml: str | None,
        summary: RepoSummary,
    ) -> None:
        job_id = int(job["id"])
        name = job.get("name")
        labels = [str(x) for x in job.get("labels") or []]
        self.store.upsert_job(
            JobRecord(
                job_id=job_id,
                run_id=run_id,
                attempt=attempt,
                name=name,
                job_key=map_job_key(name, workflow_yaml) if isinstance(name, str) else None,
                status=job.get("status"),
                conclusion=job.get("conclusion"),
                started_at=_dt(job.get("started_at")),
                completed_at=_dt(job.get("completed_at")),
                runner_os=runner_os(labels),
                labels=labels,
                raw={k: v for k, v in job.items() if k != "steps"},
            )
        )
        self.store.upsert_steps(
            [
                StepRecord(
                    job_id=job_id,
                    number=int(step["number"]),
                    name=step.get("name"),
                    status=step.get("status"),
                    conclusion=step.get("conclusion"),
                    started_at=_dt(step.get("started_at")),
                    completed_at=_dt(step.get("completed_at")),
                )
                for step in job.get("steps") or []
            ]
        )
        summary.jobs += 1
        # Every run reaching this point belongs to a relevant workflow (see _process_run).
        if should_fetch_log(job.get("status"), job.get("conclusion"), True):
            if not self.store.has_log(job_id):
                if self.logs == LOGS_FAILED and job.get("conclusion") not in FAILED_CONCLUSIONS:
                    # No logs row on purpose: `fill-logs` finds the job by the missing row.
                    summary.logs_deferred += 1
                    return
                self._store_log(entry, run_id, attempt, job_id, summary)

    def fill_logs_repo(self, entry: RepoEntry, limit: int | None = None) -> RepoSummary:
        """Pass 2: fetch logs the policy wants but the database lacks, newest runs first.

        Each log is committed on its own, so stopping at any point loses nothing, and a second
        call only sees what is still missing. Expired or unreadable logs get a status row exactly
        as in the backfill.
        """
        summary = RepoSummary()
        with self.store.transaction():
            repo = next(
                (r for r in self.store.list_repos(self.member) if r.full_name == entry.full_name),
                None,
            )
            # DEFAULT: the whole pending list is read even with a limit: logs that fail with a
            # 5xx stay pending and must not use up the limit (or they would block the rest).
            pending = self.store.pending_logs(repo.id, None) if repo else []
        fetched = 0
        for job in pending:
            if limit is not None and fetched >= limit:
                break
            failed_before = summary.logs_failed
            with self.store.transaction():
                self._store_log(entry, job.run_id, job.attempt, job.job_id, summary)
            if summary.logs_failed == failed_before:
                fetched += 1
        return summary

    def _store_log(
        self, entry: RepoEntry, run_id: int, attempt: int, job_id: int, summary: RepoSummary
    ) -> None:
        fn = entry.full_name
        now = self._clock()
        try:
            body = self.client.get_bytes(f"/repos/{fn}/actions/jobs/{job_id}/logs")
        except GitHubError as exc:
            code = exc.status_code
            if exc.is_server_error:
                # GitHub keeps failing on this log: leave it pending (no row) so `fill-logs` and
                # later runs try again, and carry on. Network and rate-limit errors still raise.
                logger.warning(
                    "log download failed with a server error; left pending",
                    extra={"repo": fn, "run_id": run_id, "job_id": job_id, "status": code},
                )
                summary.logs_failed += 1
                return
            gave_up = str(exc).startswith("gave up after retries")  # rate limit never cleared
            if code != 410 and (
                gave_up or code == 429 or not (code is not None and 400 <= code < 500)
            ):
                raise  # network and rate-limit failures stop the repo; a re-run resumes
            body = None
            status = "expired" if code == 410 else "missing"
            if code != 410:
                # DEFAULT: a non-retryable 4xx (e.g. 403) on a log is recorded as 'missing' so one
                # unreadable log does not abort the repo.
                logger.warning(
                    "log not available; recorded as missing",
                    extra={"repo": fn, "job_id": job_id, "status": code},
                )
        else:
            status = "ok" if body is not None else "missing"
        if body is None:
            self.store.upsert_log(LogRecord(job_id, None, None, None, None, status, now))
            if status == "expired":
                summary.logs_expired += 1
            else:
                summary.logs_missing += 1
            return
        result = self._sanitise(body.decode("utf-8", errors="replace"))
        data = gzip_bytes(result.text.encode("utf-8"))
        rel = f"logs/{entry.owner}/{entry.name}/{run_id}/{attempt}/{job_id}.log.gz"
        self._write_file(rel, data)
        self.store.upsert_log(
            LogRecord(
                job_id=job_id,
                storage_path=rel,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                redactions=result.total,
                status="ok",
                fetched_at=now,
            )
        )
        summary.logs_ok += 1

    def _write_file(self, relative: str, data: bytes) -> None:
        """Write under data_dir; skip when identical bytes are already there; replace atomically."""
        target = self.data_dir / relative
        if target.is_file() and target.read_bytes() == data:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        # The temp name carries the process id: lanes running side by side may write the same
        # content-addressed blob at the same moment.
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    # ---- commits ----------------------------------------------------------------------------

    def _ensure_commit(self, fn: str, repo_id: int, sha: str, summary: RepoSummary) -> None:
        if self.store.has_commit(repo_id, sha):
            return
        try:
            response = self.client.get(f"/repos/{fn}/commits/{sha}", {"per_page": 100})
        except GitHubNotFoundError:
            # DEFAULT: a commit that no longer exists (force-push) is skipped, not fatal.
            logger.warning("commit not found; diff skipped", extra={"repo": fn, "sha": sha})
            return
        body = response.json()
        files: list[dict[str, Any]] = list(body.get("files") or [])
        while next_url := response.links.get("next", {}).get("url"):
            response = self.client.get(next_url)
            files.extend(response.json().get("files") or [])
        info = body.get("commit") or {}
        author_login = (body.get("author") or {}).get("login")
        # DEFAULT: if GitHub has no account for the author, hash the commit's author email.
        identity = author_login or (info.get("author") or {}).get("email")
        message = info.get("message")
        commit = CommitRecord(
            repo_id=repo_id,
            sha=str(body.get("sha") or sha),
            parent_shas=[str(p["sha"]) for p in body.get("parents") or []],
            author_hash=self._hash(identity) if identity else None,
            committed_at=_dt((info.get("committer") or {}).get("date")),
            message=_clean(self._sanitise(message).text) if isinstance(message, str) else None,
        )
        records: dict[str, CommitFileRecord] = {}
        for f in files:
            patch = f.get("patch")
            truncated = False
            if isinstance(patch, str) and len(patch.encode("utf-8")) > self.tagger.max_patch_bytes:
                patch, truncated = None, True
            records[str(f["filename"])] = CommitFileRecord(
                repo_id=repo_id,
                sha=commit.sha,
                path=str(f["filename"]),
                previous_path=f.get("previous_filename"),
                status=f.get("status"),
                additions=f.get("additions"),
                deletions=f.get("deletions"),
                patch=_clean(self._sanitise(patch).text) if isinstance(patch, str) else None,
                patch_truncated=truncated,
            )
        self.store.upsert_commit(commit, list(records.values()))
        summary.commits += 1

    # ---- file snapshots ---------------------------------------------------------------------

    def _ensure_snapshots(
        self, fn: str, repo_id: int, sha: str, workflow_path: str, summary: RepoSummary
    ) -> None:
        """Snapshot the run's workflow file and every tagged config file the commit changed."""
        paths = {workflow_path} if workflow_path else set()
        paths.update(
            p
            for p, status in self.store.commit_files(repo_id, sha)
            if status != "removed" and self.tagger.wants_snapshot(p)
        )
        for path in sorted(paths):
            if self.store.get_snapshot(repo_id, sha, path) is not None:
                continue
            content = self.client.fetch_raw_file(fn, sha, path)
            if content is None:
                logger.warning(
                    "file not found at sha", extra={"repo": fn, "sha": sha, "path": path}
                )
                continue
            if self.tagger.is_lockfile(path) and not self.tagger.lockfiles_inline:
                inline, ref = None, self._store_blob(content)
            else:
                text = self._sanitise(content.decode("utf-8", errors="replace")).text
                inline, ref = _clean(text), None
            self.store.upsert_snapshot(
                SnapshotRecord(repo_id, sha, path, inline, ref, self._clock())
            )
            summary.snapshots += 1

    def _store_blob(self, content: bytes) -> str:
        """Store a (sanitised, gzipped) file by content hash; return its path under data_dir."""
        digest = hashlib.sha256(content).hexdigest()
        rel = f"snapshots/blobs/{digest[:2]}/{digest}.gz"
        if not (self.data_dir / rel).is_file():
            text = self._sanitise(content.decode("utf-8", errors="replace")).text
            self._write_file(rel, gzip_bytes(text.encode("utf-8")))
        return rel
