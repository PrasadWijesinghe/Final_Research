"""POST /webhooks/github: signed fake payloads drive the live flows; GitHub is a respx mock."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from api.app import create_app
from api.raw_store import InMemoryRawStore
from api.router import FailedJob
from api.store import InMemoryStore
from api.webhooks import WebhookHandler
from api_helpers import mock_experts, settings

from shared.contracts import Expert

pytestmark = pytest.mark.anyio

SECRET = "fake-webhook-secret-for-tests"
GH = "https://api.github.com"
SHA = "b" * 40
BASE = "a" * 40
FAKE_PAT = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"  # obviously fake, but pattern-shaped


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeTokens:
    def __init__(self) -> None:
        self.asked: list[int] = []

    def get_token(self, installation_id: int) -> str:
        self.asked.append(installation_id)
        return "ghs_faketoken"


class Rig:
    def __init__(self, tmp_path: Path, mock: respx.MockRouter) -> None:
        self.store = InMemoryStore()
        self.raw = InMemoryRawStore()
        self.tokens = FakeTokens()
        self.routed: list[FailedJob] = []
        self.mock = mock
        self.data_dir = tmp_path
        cfg = settings(github_webhook_secret=SECRET, data_dir=str(tmp_path))
        handler = WebhookHandler(cfg, self.store, self.raw, self.tokens, self.routed.append)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(cfg, self.store, handler)),
            base_url="http://api.test",
        )

    async def send(
        self, event: str, payload: dict[str, Any], *, secret: str = SECRET
    ) -> httpx.Response:
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return await self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": "fake-delivery",
                "Content-Type": "application/json",
            },
        )


@pytest.fixture
def mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.setenv("IDENTITY_SALT", "test-salt-not-secret")
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def rig(tmp_path: Path, mock: respx.MockRouter) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, mock)
    async with r.client:
        yield r


def commit_body(sha: str = SHA) -> dict[str, Any]:
    return {
        "sha": sha,
        "parents": [{"sha": BASE}],
        "author": {"login": "dev-user"},
        "commit": {
            "author": {"email": "dev@example.test"},
            "committer": {"date": "2026-10-01T10:00:00Z"},
            "message": "change a",
        },
        "files": [
            {"filename": "src/a.js", "status": "modified", "additions": 2, "deletions": 1,
             "patch": "@@ -1 +1,2 @@\n-a\n+b\n+c"}
        ],
    }  # fmt: skip


def mock_github_commit(mock: respx.MockRouter) -> respx.Route:
    route = mock.get(f"{GH}/repos/acme/widgets/commits/{SHA}").respond(200, json=commit_body())
    mock.get(f"{GH}/repos/acme/widgets/compare/{BASE}...{SHA}").respond(200, json=commit_body())
    mock.get(f"{GH}/repos/acme/widgets/contents/.github/workflows").respond(404, json={})
    return route


REPO = {"id": 99, "full_name": "acme/widgets", "default_branch": "main"}
INSTALL = {"id": 4242}


def push_payload(**kw: Any) -> dict[str, Any]:
    return {
        "ref": "refs/heads/feature/x",
        "after": SHA,
        "before": BASE,
        "repository": REPO,
        "installation": INSTALL,
        **kw,
    }


# ---------------------------------------------------------------------------- signatures


async def test_bad_signature_is_401_and_does_nothing(rig: Rig) -> None:
    r = await rig.send("push", push_payload(), secret="wrong-secret")
    assert r.status_code == 401
    assert not rig.store.contexts and not rig.tokens.asked


async def test_missing_signature_is_401(rig: Rig) -> None:
    r = await rig.client.post("/webhooks/github", json=push_payload())
    assert r.status_code == 401


async def test_no_secret_configured_is_503(mock: respx.MockRouter) -> None:
    app = create_app(settings(github_webhook_secret=""), InMemoryStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        assert (await c.post("/webhooks/github", json={})).status_code == 503


async def test_ping_is_answered(rig: Rig) -> None:
    r = await rig.send("ping", {"zen": "fake"})
    assert r.status_code == 202 and r.json() == {"status": "pong"}


# ------------------------------------------------------------------- push / pull_request


async def test_push_builds_context_and_runs_predict(rig: Rig) -> None:
    commit_route = mock_github_commit(rig.mock)
    seen: list[httpx.Request] = []
    mock_experts(rig.mock, seen=seen)
    rig.mock.post(f"{GH}/repos/acme/widgets/check-runs").respond(201, json={"id": 1})
    r = await rig.send("push", push_payload())
    assert r.status_code == 202 and r.json()["event"] == "push"
    assert rig.tokens.asked == [4242, 4242]  # one token for the commit data, one for the check
    assert commit_route.calls[0].request.headers["Authorization"] == "Bearer ghs_faketoken"
    ctx = next(iter(rig.store.contexts.values()))
    assert ctx.event == "push" and ctx.head_sha == SHA and ctx.branch == "feature/x"
    assert ctx.repo.full_name == "acme/widgets"
    assert len(seen) == 4 and {s.headers["X-Deadline-Ms"] for s in seen} == {"120000"}
    saved = {risk.expert for risk, _ in rig.store.predictions.values()}
    assert saved == set(Expert)


async def test_push_branch_deletion_and_tags_are_ignored(rig: Rig) -> None:
    await rig.send("push", push_payload(deleted=True, after="0" * 40))
    await rig.send("push", push_payload(ref="refs/tags/v1"))
    assert not rig.store.contexts and not rig.tokens.asked


async def test_pull_request_uses_base_and_pr_number(rig: Rig) -> None:
    mock_github_commit(rig.mock)
    mock_experts(rig.mock)
    payload = {
        "action": "opened",
        "number": 7,
        "pull_request": {
            "number": 7,
            "head": {"sha": SHA, "ref": "feature/x"},
            "base": {"sha": BASE},
        },
        "repository": REPO,
        "installation": INSTALL,
    }  # fmt: skip
    assert (await rig.send("pull_request", payload)).status_code == 202
    ctx = next(iter(rig.store.contexts.values()))
    assert ctx.event == "pull_request" and ctx.pr_number == 7 and ctx.base_sha == BASE
    assert len(rig.store.predictions) == 4


async def test_pull_request_other_actions_are_ignored(rig: Rig) -> None:
    payload = {"action": "labeled", "pull_request": {}, "repository": REPO, "installation": INSTALL}
    assert (await rig.send("pull_request", payload)).status_code == 202
    assert not rig.store.contexts and not rig.tokens.asked


async def test_handler_error_does_not_break_the_delivery(rig: Rig) -> None:
    rig.mock.get(f"{GH}/repos/acme/widgets/commits/{SHA}").respond(404, json={})
    r = await rig.send("push", push_payload())
    assert r.status_code == 202  # logged, GitHub is not told to retry
    assert not rig.store.contexts


# ------------------------------------------------------------------------ raw storage


def run_payload(**kw: Any) -> dict[str, Any]:
    run = {
        "id": 555,
        "run_attempt": 2,
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "path": ".github/workflows/ci.yml@refs/heads/main",
        "created_at": "2026-10-01T10:00:00Z",
        "run_started_at": "2026-10-01T10:00:05Z",
        "updated_at": "2026-10-01T10:05:00Z",
        "display_title": f"fix token {FAKE_PAT}",
        "actor": {"login": "dev-user", "email": "dev@example.test"},
        "head_commit": {"author": {"email": "dev@example.test"}},
        "pull_requests": [{"number": 7}],
        **kw,
    }
    return {
        "action": "completed",
        "workflow_run": run,
        "repository": REPO,
        "installation": INSTALL,
    }


async def test_workflow_run_is_stored_sanitised(rig: Rig) -> None:
    assert (await rig.send("workflow_run", run_payload())).status_code == 202
    run = rig.raw.runs[(555, 2)]
    assert run.workflow_path == ".github/workflows/ci.yml" and run.pr_numbers == [7]
    assert run.conclusion == "failure" and rig.raw.repos == {99: "acme/widgets"}
    assert run.actor_hash and "dev-user" not in run.actor_hash
    assert run.raw is not None and "actor" not in run.raw and "head_commit" not in run.raw
    dumped = json.dumps(run.raw)
    assert FAKE_PAT not in dumped and "dev@example.test" not in dumped


def job_payload(conclusion: str | None = "failure") -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_job": {
            "id": 777,
            "run_id": 555,
            "run_attempt": 2,
            "name": "test (20)",
            "head_sha": SHA,
            "status": "completed",
            "conclusion": conclusion,
            "started_at": "2026-10-01T10:00:10Z",
            "completed_at": "2026-10-01T10:04:00Z",
            "labels": ["ubuntu-latest"],
            "steps": [
                {"number": 1, "name": "checkout", "status": "completed", "conclusion": "success"},
                {"number": 2, "name": "npm test", "status": "completed", "conclusion": "failure",
                 "started_at": "2026-10-01T10:01:00Z", "completed_at": "2026-10-01T10:03:00Z"},
            ],
        },
        "repository": REPO,
        "installation": INSTALL,
    }  # fmt: skip


async def test_failed_job_is_stored_logged_sanitised_and_routed(rig: Rig) -> None:
    log = f"FAIL src/a.test.js\nAuthorization: {FAKE_PAT}\n"
    rig.mock.get(f"{GH}/repos/acme/widgets/actions/jobs/777/logs").respond(200, text=log)
    rig.mock.get(f"{GH}/repos/acme/widgets/actions/runs/555").respond(
        200, json={"path": ".github/workflows/ci.yml@refs/heads/main"}
    )
    assert (await rig.send("workflow_job", job_payload())).status_code == 202
    job = rig.raw.jobs[777]
    assert job.attempt == 2 and job.runner_os == "Linux" and len(job.steps) == 2
    assert job.raw is not None and "steps" not in job.raw
    stored = rig.raw.logs[777]
    text = gzip.decompress((rig.data_dir / stored.storage_path).read_bytes()).decode()
    assert "FAIL src/a.test.js" in text and FAKE_PAT not in text and "[REDACTED" in text
    assert stored.redactions >= 1
    (routed,) = rig.routed
    assert routed.job_id == 777 and routed.run_attempt == 2 and routed.head_sha == SHA
    assert routed.workflow_path == ".github/workflows/ci.yml"
    assert [s.name for s in routed.failed_steps] == ["npm test"]
    assert FAKE_PAT not in routed.log_excerpt and routed.log_ref == stored.storage_path


async def test_passing_job_is_stored_but_not_routed(rig: Rig) -> None:
    assert (await rig.send("workflow_job", job_payload("success"))).status_code == 202
    assert 777 in rig.raw.jobs and not rig.routed and not rig.raw.logs


async def test_in_progress_job_updates_the_same_row(rig: Rig) -> None:
    p = job_payload(None)
    p["action"] = "in_progress"
    p["workflow_job"]["status"] = "in_progress"
    await rig.send("workflow_job", p)
    assert rig.raw.jobs[777].status == "in_progress" and not rig.routed
    await rig.send("workflow_job", job_payload("success"))
    assert rig.raw.jobs[777].conclusion == "success" and len(rig.raw.jobs) == 1
