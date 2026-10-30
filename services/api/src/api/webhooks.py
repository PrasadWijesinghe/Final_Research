"""GitHub App webhook handling: push and pull_request go to the live predict flow, workflow_run and
workflow_job are stored in `raw` (source="app"), and a failed job is routed for diagnosis."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from features.live import build_live_context

from shared.contracts import CommitContext, Expert, RepoRef, RiskOutput
from shared.contracts.routed_failure import FailedStep
from shared.github import GitHubClient, GitHubError, TokenProvider
from shared.sanitise import SanitiseResult, hash_identity, sanitise

from .checks import CheckPublisher
from .config import Settings
from .experts import fan_out_predict
from .raw_store import AppJob, AppLog, AppRun, RawStore
from .router import FailedJob
from .store import Store

logger = logging.getLogger(__name__)

# DEFAULT: PR actions that change the code under review; others (labeled, closed...) are ignored.
PR_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
# DEFAULT: same excerpt size as replay (the contract's maximum); logs over 50 MB are not fetched.
EXCERPT_CHARS = 20_000
MAX_LOG_BYTES = 50 * 1024 * 1024
# Same keys the collector drops from a run's raw JSON (they carry logins, emails and commit text).
RUN_RAW_DROP = ("actor", "triggering_actor", "head_commit", "repository", "head_repository")
JOB_RAW_DROP = ("steps",)
FAILED_STEP = ("failure", "cancelled", "timed_out")
_NUL = "\x00"
_ZERO_SHA = "0" * 40

RouteFn = Callable[[FailedJob], None]


def _dt(value: Any) -> datetime | None:
    return (
        datetime.fromisoformat(value).astimezone(UTC) if isinstance(value, str) and value else None
    )


def _runner_os(labels: list[str]) -> str | None:
    for label in labels:
        low = label.lower()
        for needle, name in (("ubuntu", "Linux"), ("linux", "Linux"), ("windows", "Windows")):
            if needle in low:
                return name
        if "macos" in low or "mac-os" in low:
            return "macOS"
    return None


class WebhookHandler:
    """Turns verified webhook payloads into stored rows, predictions and routed failures."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        raw: RawStore,
        tokens: TokenProvider,
        route: RouteFn,
        *,
        client_factory: Callable[[str], GitHubClient] = lambda token: GitHubClient(token=token),
        hash_fn: Callable[[str], str] = hash_identity,
        sanitise_fn: Callable[[str], SanitiseResult] = sanitise,
    ) -> None:
        self._cfg = settings
        self._store = store
        self._raw = raw
        self._tokens = tokens
        self._route = route
        self._client_factory = client_factory
        self._hash = hash_fn
        self._sanitise = sanitise_fn
        self._late: set[asyncio.Task[None]] = set()

    async def handle(self, event: str, payload: dict[str, Any]) -> str:
        """Process one delivery; returns a short outcome word (logged, never the payload)."""
        if event == "push":
            return await self._push(payload)
        if event == "pull_request":
            return await self._pull_request(payload)
        if event == "workflow_run":
            return await asyncio.to_thread(self._workflow_run, payload)
        if event == "workflow_job":
            return await self._workflow_job(payload)
        return "ignored"

    # ----------------------------------------------------------------- live predict

    def _client(self, payload: dict[str, Any]) -> GitHubClient | None:
        installation = (payload.get("installation") or {}).get("id")
        if not installation:
            return None
        return self._client_factory(self._tokens.get_token(int(installation)))

    async def _predict(self, payload: dict[str, Any], **ctx: Any) -> str:
        repo = str(payload["repository"]["full_name"])

        def build() -> CommitContext | None:
            client = self._client(payload)
            if client is None:
                return None
            with client:
                return build_live_context(client, repo=repo, **ctx)

        context = await asyncio.to_thread(build)
        if context is None:
            logger.warning("webhook without installation id", extra={"repo": repo})
            return "no_installation"
        await asyncio.to_thread(self._store.save_context, context)
        results = await fan_out_predict(self._cfg, context, self._cfg.pr_budget_ms)
        ready = [risk for r in results for risk in r.risks]
        if ready:
            await asyncio.to_thread(self._store.save_predictions, ready)
        pending = [r.expert for r in results if r.state != "ready"]
        installation = int(payload["installation"]["id"])
        await self._publish_check(installation, repo, context.head_sha, ready, pending)
        if pending and self._cfg.check_retry_delays_s:
            task = asyncio.create_task(
                self._poll_late(installation, repo, context, ready, pending),
                name=f"late-results-{context.head_sha[:7]}",
            )
            self._late.add(task)
            task.add_done_callback(self._late.discard)
        logger.info(
            "live predict done",
            extra={"context_id": str(context.context_id), "event": context.event},
        )
        return "predicted"

    # --------------------------------------------------------------------- PR check (P3.3)

    async def _publish_check(
        self,
        installation: int,
        repo: str,
        head_sha: str,
        risks: list[RiskOutput],
        pending: list[Expert],
        *,
        final: bool = False,
    ) -> None:
        """Create or update the check run. Never raises: a GitHub problem must not stop webhooks."""
        publisher = CheckPublisher(
            self._store,
            self._cfg.dashboard_url,
            lambda: self._client_factory(self._tokens.get_token(installation)),
        )
        try:
            await asyncio.to_thread(publisher.publish, repo, head_sha, risks, pending, final=final)
        except Exception:
            logger.exception("check run failed", extra={"repo": repo, "sha": head_sha})

    async def _poll_late(
        self,
        installation: int,
        repo: str,
        context: CommitContext,
        ready: list[RiskOutput],
        pending: list[Expert],
    ) -> None:
        """Ask the experts that were not ready again, a bounded number of times, and update the
        same check run with each new answer. The last round finishes the check either way.

        # DEFAULT: no queue or callback from the experts exists yet (/predict is synchronous), so
        # late results come from re-polling /predict after CHECK_RETRY_DELAYS_SECONDS.
        """
        risks = list(ready)
        delays = self._cfg.check_retry_delays_s
        try:
            for i, delay in enumerate(delays):
                await asyncio.sleep(delay)
                results = await fan_out_predict(
                    self._cfg, context, self._cfg.pr_budget_ms, experts=pending
                )
                new = [risk for r in results for risk in r.risks]
                if new:
                    await asyncio.to_thread(self._store.save_predictions, new)
                    risks += new
                pending = [r.expert for r in results if r.state != "ready"]
                last = i == len(delays) - 1
                if new or last or not pending:
                    await self._publish_check(
                        installation, repo, context.head_sha, risks, pending, final=last
                    )
                if not pending:
                    return
        except Exception:
            logger.exception("late results failed", extra={"repo": repo})

    async def wait_idle(self) -> None:
        """Wait for the late-result tasks to finish (tests and shutdown)."""
        while self._late:
            await asyncio.gather(*list(self._late), return_exceptions=True)

    async def _push(self, p: dict[str, Any]) -> str:
        ref = str(p.get("ref", ""))
        after = str(p.get("after", ""))
        if p.get("deleted") or not ref.startswith("refs/heads/") or not after or after == _ZERO_SHA:
            return "ignored"
        return await self._predict(
            p, head_sha=after, event="push", branch=ref.removeprefix("refs/heads/")
        )

    async def _pull_request(self, p: dict[str, Any]) -> str:
        if p.get("action") not in PR_ACTIONS:
            return "ignored"
        pr = p["pull_request"]
        return await self._predict(
            p,
            head_sha=str(pr["head"]["sha"]),
            base_sha=str(pr["base"]["sha"]),
            event="pull_request",
            branch=str(pr["head"]["ref"]),
            pr_number=int(pr["number"]),
        )

    # ------------------------------------------------------------------ raw storage

    def _ensure_repo(self, payload: dict[str, Any]) -> int:
        repo = payload["repository"]
        repo_id = int(repo["id"])
        self._raw.upsert_repo(repo_id, str(repo["full_name"]), repo.get("default_branch"))
        return repo_id

    def _clean(self, text: str) -> str:
        return self._sanitise(text).text.replace(_NUL, "")

    def _workflow_run(self, p: dict[str, Any]) -> str:
        run = p["workflow_run"]
        repo_id = self._ensure_repo(p)
        actor = (run.get("actor") or {}).get("login")
        raw = {k: v for k, v in run.items() if k not in RUN_RAW_DROP}
        if isinstance(raw.get("display_title"), str):
            raw["display_title"] = self._clean(raw["display_title"])
        self._raw.upsert_run(
            AppRun(
                run_id=int(run["id"]),
                attempt=int(run.get("run_attempt") or 1),
                repo_id=repo_id,
                workflow_path=str(run.get("path") or "").split("@", 1)[0] or None,
                head_sha=str(run["head_sha"]),
                head_branch=run.get("head_branch"),
                event=run.get("event"),
                status=run.get("status"),
                conclusion=run.get("conclusion"),
                created_at=_dt(run.get("created_at")),
                run_started_at=_dt(run.get("run_started_at")),
                updated_at=_dt(run.get("updated_at")),
                actor_hash=self._hash(actor) if actor else None,
                pr_numbers=[
                    int(x["number"]) for x in run.get("pull_requests") or [] if "number" in x
                ],
                raw=raw,
            )
        )
        return "stored"

    def _store_job(self, p: dict[str, Any]) -> AppJob:
        job = p["workflow_job"]
        self._ensure_repo(p)
        labels = [str(x) for x in job.get("labels") or []]
        record = AppJob(
            job_id=int(job["id"]),
            run_id=int(job["run_id"]),
            attempt=int(job.get("run_attempt") or 1),
            name=job.get("name"),
            status=job.get("status"),
            conclusion=job.get("conclusion"),
            started_at=_dt(job.get("started_at")),
            completed_at=_dt(job.get("completed_at")),
            runner_os=_runner_os(labels),
            labels=labels,
            raw={k: v for k, v in job.items() if k not in JOB_RAW_DROP},
            steps=[
                {
                    "number": int(s["number"]),
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "conclusion": s.get("conclusion"),
                    "started_at": _dt(s.get("started_at")),
                    "completed_at": _dt(s.get("completed_at")),
                }
                for s in job.get("steps") or []
            ],
        )
        self._raw.upsert_job(record)
        return record

    async def _workflow_job(self, p: dict[str, Any]) -> str:
        job = await asyncio.to_thread(self._store_job, p)
        if p.get("action") != "completed" or job.conclusion != "failure":
            return "stored"
        routed = await asyncio.to_thread(self._route_failed, p, job)
        return "routed" if routed else "stored"

    # ------------------------------------------------------------- failure routing

    def _route_failed(self, p: dict[str, Any], job: AppJob) -> bool:
        """Fetch the failed job's log, sanitise and store it, then route it. False if skipped."""
        failed = [
            FailedStep(number=int(s["number"]), name=str(s.get("name")), conclusion=s["conclusion"])
            for s in (p["workflow_job"].get("steps") or [])
            if s.get("conclusion") in FAILED_STEP
        ]
        client = self._client(p)
        if not failed or client is None:
            return False
        repo = p["repository"]
        full_name = str(repo["full_name"])
        owner, _, name = full_name.partition("/")
        try:
            with client:
                body = client.get_bytes(
                    f"/repos/{full_name}/actions/jobs/{job.job_id}/logs", max_bytes=MAX_LOG_BYTES
                )
                run = client.get_json(f"/repos/{full_name}/actions/runs/{job.run_id}")
        except GitHubError:
            logger.warning("could not fetch failed job", extra={"job_id": job.job_id})
            return False
        if body is None:
            return False
        result = self._sanitise(body.decode("utf-8", errors="replace"))
        text = result.text.replace(_NUL, "")
        data = gzip.compress(text.encode("utf-8"), mtime=0)
        rel = f"logs/{owner}/{name}/{job.run_id}/{job.attempt}/{job.job_id}.log.gz"
        target = Path(self._cfg.data_dir or Path.cwd() / "data") / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._raw.upsert_log(
            AppLog(
                job_id=job.job_id,
                storage_path=rel,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                redactions=result.total,
                fetched_at=datetime.now(UTC),
            )
        )
        wjob = p["workflow_job"]
        self._route(
            FailedJob(
                repo=RepoRef(owner=owner, name=name),
                head_sha=str(wjob["head_sha"]),
                run_id=job.run_id,
                run_attempt=job.attempt,
                job_id=job.job_id,
                job_name=str(job.name),
                workflow_path=str(run.get("path") or "").split("@", 1)[0],
                failed_steps=failed,
                log_ref=rel,
                log_excerpt=text[-EXCERPT_CHARS:],
            )
        )
        return True
