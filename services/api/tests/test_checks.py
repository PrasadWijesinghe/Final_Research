"""PR check run (P3.3): create on first results, PATCH the same check on late ones; GitHub is a
respx mock, the experts are fake routes, and replay never reaches this code."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from api.checks import MAX_ROWS_PER_EXPERT, render_check
from api.raw_store import InMemoryRawStore
from api.store import InMemoryStore
from api.webhooks import WebhookHandler
from api_helpers import mock_experts, risk_for, settings

from shared.contracts import Expert, RiskFactor, RiskOutput

pytestmark = pytest.mark.anyio

GH = "https://api.github.com"
SHA = "b" * 40
BASE = "a" * 40
CHECKS = f"{GH}/repos/acme/widgets/check-runs"
PAYLOAD = {
    "ref": "refs/heads/feature/x",
    "after": SHA,
    "before": BASE,
    "repository": {"id": 99, "full_name": "acme/widgets", "default_branch": "main"},
    "installation": {"id": 4242},
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeTokens:
    def get_token(self, installation_id: int) -> str:
        return "ghs_faketoken"


@pytest.fixture
def mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.setenv("IDENTITY_SALT", "test-salt-not-secret")
    with respx.mock(assert_all_called=False) as router:
        commit = {
            "sha": SHA,
            "parents": [{"sha": BASE}],
            "author": {"login": "dev-user"},
            "commit": {
                "author": {"email": "dev@example.test"},
                "committer": {"date": "2026-10-01T10:00:00Z"},
                "message": "change a",
            },
            "files": [{"filename": "src/a.js", "status": "modified", "additions": 1,
                       "deletions": 0, "patch": "@@ -1 +1 @@\n-a\n+b"}],
        }  # fmt: skip
        router.get(f"{GH}/repos/acme/widgets/commits/{SHA}").respond(200, json=commit)
        router.get(f"{GH}/repos/acme/widgets/compare/{BASE}...{SHA}").respond(200, json=commit)
        router.get(f"{GH}/repos/acme/widgets/contents/.github/workflows").respond(404, json={})
        yield router


def make_handler(store: InMemoryStore, tmp_path: Path, delays: tuple[float, ...]) -> WebhookHandler:
    cfg = settings(
        data_dir=str(tmp_path), check_retry_delays_s=delays, dashboard_url="https://dash.test"
    )
    return WebhookHandler(cfg, store, InMemoryRawStore(), FakeTokens(), lambda job: None)


def body(call: Any) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(call.request.content)
    return parsed


def alerting(expert: Expert, context: dict[str, Any]) -> dict[str, Any]:
    out = RiskOutput(
        context_id=context["context_id"],
        expert=expert,
        model_version="stub",
        scope="commit",
        probability=0.83,
        threshold=0.5,
        alert=True,
        latency_ms=3,
        top_factors=[
            RiskFactor(
                feature="f",
                value=1,
                shap_value=0.4,
                direction="raises",
                reason="Changes src/a.js, which broke before",
            )
        ],
    )
    dumped: dict[str, Any] = out.model_dump(mode="json")
    return dumped


async def test_first_results_create_one_success_check(
    mock: respx.MockRouter, tmp_path: Path
) -> None:
    mock_experts(mock)
    route = mock.post(CHECKS).respond(201, json={"id": 555})
    store = InMemoryStore()
    assert await make_handler(store, tmp_path, ()).handle("push", PAYLOAD) == "predicted"
    assert route.call_count == 1
    sent = body(route.calls[0])
    assert sent["name"] == "PipelineIQ" and sent["head_sha"] == SHA
    assert sent["status"] == "completed" and sent["conclusion"] == "success"
    assert sent["details_url"] == f"https://dash.test/#/commits/{SHA}"
    assert route.calls[0].request.headers["Authorization"] == "Bearer ghs_faketoken"
    assert store.get_check_run("acme/widgets", SHA) == 555


async def test_alerts_make_it_neutral_with_scores_and_factors(
    mock: respx.MockRouter, tmp_path: Path
) -> None:
    mock_experts(
        mock,
        overrides={
            Expert.TEST: lambda req: httpx.Response(
                200, json=[alerting(Expert.TEST, json.loads(req.content))]
            )
        },
    )
    route = mock.post(CHECKS).respond(201, json={"id": 1})
    await make_handler(InMemoryStore(), tmp_path, ()).handle("push", PAYLOAD)
    sent = body(route.calls[0])
    assert sent["conclusion"] == "neutral"
    assert sent["output"]["title"] == "PipelineIQ: 1 alert (Test)"
    summary = sent["output"]["summary"]
    assert "**Test**: ALERT, score 0.83 (alert at 0.50)" in summary
    assert "Changes src/a.js, which broke before" in summary
    assert "ghs_" not in summary


async def test_nothing_ready_creates_in_progress(mock: respx.MockRouter, tmp_path: Path) -> None:
    mock_experts(mock, overrides={e: lambda req: httpx.Response(503) for e in Expert})
    route = mock.post(CHECKS).respond(201, json={"id": 7})
    await make_handler(InMemoryStore(), tmp_path, ()).handle("push", PAYLOAD)
    sent = body(route.calls[0])
    assert sent["status"] == "in_progress" and "conclusion" not in sent
    assert "Still pending: Dependency, Build, Test, Infra." in sent["output"]["summary"]


async def test_late_result_patches_the_same_check(mock: respx.MockRouter, tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=[risk_for(Expert.TEST, json.loads(req.content))])

    mock_experts(mock, overrides={Expert.TEST: flaky})
    post = mock.post(CHECKS).respond(201, json={"id": 555})
    patch = mock.patch(f"{CHECKS}/555").respond(200, json={"id": 555})
    store = InMemoryStore()
    handler = make_handler(store, tmp_path, (0.0, 0.0))
    await handler.handle("push", PAYLOAD)
    first = body(post.calls[0])
    assert first["conclusion"] == "neutral"  # one expert missing
    assert "Still pending: Test." in first["output"]["summary"]
    await handler.wait_idle()
    assert post.call_count == 1 and patch.call_count == 1
    last = body(patch.calls[0])
    assert last["conclusion"] == "success" and "pending" not in last["output"]["summary"]
    assert calls["n"] == 2  # polling stopped once the expert answered
    assert len([r for r, _ in store.predictions.values() if r.expert == Expert.TEST]) == 1


async def test_never_ready_is_finished_neutral_after_the_window(
    mock: respx.MockRouter, tmp_path: Path
) -> None:
    mock_experts(mock, overrides={e: lambda req: httpx.Response(503) for e in Expert})
    post = mock.post(CHECKS).respond(201, json={"id": 9})
    patch = mock.patch(f"{CHECKS}/9").respond(200, json={})
    handler = make_handler(InMemoryStore(), tmp_path, (0.0, 0.0))
    await handler.handle("push", PAYLOAD)
    await handler.wait_idle()
    assert post.call_count == 1 and patch.call_count == 1
    done = body(patch.calls[0])
    assert done["status"] == "completed" and done["conclusion"] == "neutral"
    assert "did not answer in time" in done["output"]["summary"]


async def test_same_commit_pushed_again_updates_instead_of_creating(
    mock: respx.MockRouter, tmp_path: Path
) -> None:
    mock_experts(mock)
    post = mock.post(CHECKS).respond(201, json={"id": 3})
    patch = mock.patch(f"{CHECKS}/3").respond(200, json={})
    handler = make_handler(InMemoryStore(), tmp_path, ())
    await handler.handle("push", PAYLOAD)
    await handler.handle("push", PAYLOAD)
    assert post.call_count == 1 and patch.call_count == 1


async def test_github_5xx_is_swallowed_and_logged(
    mock: respx.MockRouter, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mock_experts(mock)
    mock.post(CHECKS).respond(502, json={"message": "Bad Gateway"})
    store = InMemoryStore()
    with caplog.at_level(logging.WARNING):
        outcome = await make_handler(store, tmp_path, ()).handle("push", PAYLOAD)
    assert outcome == "predicted"
    assert store.get_check_run("acme/widgets", SHA) is None
    assert any("check run not posted" in r.getMessage() for r in caplog.records)
    assert all("ghs_faketoken" not in r.getMessage() for r in caplog.records)
    assert len(store.predictions) == 4  # predictions are kept


def make_risks(count: int, expert: Expert = Expert.TEST) -> list[RiskOutput]:
    ctx = {"context_id": "00000000-0000-0000-0000-000000000001"}
    out: list[RiskOutput] = []
    for i in range(count):
        data = risk_for(expert, ctx)
        data.update(scope="job", workflow_path="ci.yml", job_key=f"job-{i}|x\n", probability=0.1)
        out.append(RiskOutput.model_validate(data))
    return out


def test_render_caps_rows_and_cleans_text() -> None:
    fields = render_check(
        make_risks(MAX_ROWS_PER_EXPERT + 5), [], head_sha=SHA, dashboard_url="https://d.test/"
    )
    summary = fields["output"]["summary"]
    assert summary.count("**Test**") == MAX_ROWS_PER_EXPERT
    assert "5 more not shown" in summary
    assert "|" not in summary and "\x00" not in summary
    assert fields["details_url"] == f"https://d.test/#/commits/{SHA}"


def test_render_error_risk_and_pending_list() -> None:
    risk = make_risks(1, Expert.BUILD)[0].model_copy(
        update={"status": "error", "probability": None, "error_message": "boom"}
    )
    fields = render_check([risk], [Expert.TEST], head_sha=SHA, dashboard_url="https://d.test")
    assert "no score (error)" in fields["output"]["summary"]
    assert "Still pending: Test." in fields["output"]["summary"]
    assert fields["conclusion"] == "neutral"


def test_replay_never_loads_the_check_code() -> None:
    code = (
        "import sys, api.replay\n"
        "bad = [n for n in sys.modules if n in ('api.checks', 'api.webhooks')]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
