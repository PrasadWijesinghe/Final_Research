"""Replay CLI: stored runs in, flagged predictions and routed failures out, GitHub never touched."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import pytest
import respx
from api.failure_queue import queue_name
from api.replay import (
    InMemoryReplaySource,
    ReplayRun,
    build_parser,
    experts_for_member,
    replay_window,
)
from api.router import FailedJob
from api.store import InMemoryStore
from api_helpers import URLS, risk_for, settings
from features.replay import CommitFileRow, CommitRow, InMemoryHistory, RunRow
from rq import Queue

from shared.contracts import Expert, RepoRef
from shared.contracts.routed_failure import FailedStep

REPO = "acme/widgets"
START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 4, 1, tzinfo=UTC)
CI = ".github/workflows/ci.yml"


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only."""
    return "asyncio"


def sha(n: int) -> str:
    """A fake 40-character commit sha."""
    return f"{n:040x}"


def world() -> tuple[InMemoryHistory, InMemoryReplaySource]:
    """Runs 1 and 2 inside the window (run 1 failed a job), run 3 after it, run 4 unreadable."""
    h = InMemoryHistory()
    src = InMemoryReplaySource()
    for run_id, day in ((1, 5), (2, 9), (3, 40), (4, 12)):
        created = START + timedelta(days=day, hours=1)
        row = RunRow(run_id, 1, sha(run_id), created, CI, "main", "c" * 64)
        h.runs.append((REPO, row))
        src.runs.append(ReplayRun(REPO, row))
        if run_id != 4:  # run 4 has no commit row: the replay context cannot be built
            h.commits[(REPO, sha(run_id))] = CommitRow(
                sha(run_id), created - timedelta(minutes=5), "d" * 64, (sha(0),)
            )
            h.files[(REPO, sha(run_id))] = [CommitFileRow("src/a.ts", "modified", 1, 0, "@@ +x")]
    src.jobs[(REPO, 1, 1)] = [
        FailedJob(
            repo=RepoRef(owner="acme", name="widgets"),
            head_sha=sha(1),
            run_id=1,
            job_id=10,
            job_name="test",
            workflow_path=CI,
            failed_steps=[FailedStep(number=4, name="npm test", conclusion="failure")],
            log_ref="logs/acme-widgets/1/10.log.gz",
            log_excerpt="FAIL test/a.test.ts\n  expect(received).toBe(expected)\nTests: 1 failed",
        )
    ]
    return h, src


def mock_test_expert(router: respx.MockRouter, seen: list[httpx.Request]) -> None:
    """Only the test expert answers; any other expert URL would fail the test (no route)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[risk_for(Expert.TEST, json.loads(request.content))])

    router.post(f"{URLS[Expert.TEST]}/predict").mock(side_effect=handler)


async def test_replay_stores_flagged_predictions_and_routed_failures() -> None:
    h, src = world()
    store, conn = InMemoryStore(), fakeredis.FakeRedis()
    seen: list[httpx.Request] = []
    with respx.mock(assert_all_called=False) as router:
        # Anything addressed to a GitHub host would hit this route and fail the test.
        github = router.route(host__regex=r".*github.*").mock(
            side_effect=AssertionError("replay must not call GitHub")
        )
        mock_test_expert(router, seen)
        summary = await replay_window(
            source=src,
            history=h,
            store=store,
            settings=settings(),
            connection=conn,
            experts=[Expert.TEST],
            start=START,
            end=END,
        )
        assert not github.called

    assert (summary.runs, summary.skipped_runs) == (2, 1)
    assert summary.predictions == 2
    assert (summary.failures_routed, summary.failures_unrouted) == (1, 0)
    # only the member's expert was called, once per in-window run
    assert len(seen) == 2
    ctxs = list(store.contexts.values())
    assert {c.event for c in ctxs} == {"replay"}
    assert {c.head_sha for c in ctxs} == {sha(1), sha(2)}  # run 3 is outside, run 4 unreadable
    # every stored prediction is flagged as replay
    assert len(store.predictions) == 2
    assert all(is_replay for _, is_replay in store.predictions.values())
    # the failed job was routed to the test expert, flagged, linked to its run's context
    (failure,) = store.failures.values()
    assert failure.replay is True
    assert failure.sent_to == [Expert.TEST]
    assert failure.run_id == 1
    assert failure.context_id in {c.context_id for c in ctxs}
    # ... and queued for the worker
    queue = Queue(queue_name(Expert.TEST), connection=conn)
    assert queue.count == 1
    assert queue.jobs[0].args[0]["replay"] is True


async def test_replay_window_excludes_runs_outside() -> None:
    h, src = world()
    store = InMemoryStore()
    with respx.mock(assert_all_called=False) as router:
        mock_test_expert(router, [])
        summary = await replay_window(
            source=src,
            history=h,
            store=store,
            settings=settings(),
            connection=fakeredis.FakeRedis(),
            experts=[Expert.TEST],
            start=START + timedelta(days=30),
            end=START + timedelta(days=50),
        )
    assert summary.runs == 1  # run 3 only
    assert not store.failures


async def test_replay_survives_a_dead_expert() -> None:
    h, src = world()
    store = InMemoryStore()
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{URLS[Expert.TEST]}/predict").mock(side_effect=httpx.ConnectError("down"))
        summary = await replay_window(
            source=src,
            history=h,
            store=store,
            settings=settings(),
            connection=fakeredis.FakeRedis(),
            experts=[Expert.TEST],
            start=START,
            end=END,
        )
    assert summary.experts_not_ready == 2
    assert summary.predictions == 0
    assert summary.failures_routed == 1  # routing does not depend on the experts being up


def test_member_maps_to_experts() -> None:
    assert experts_for_member("test") == [Expert.TEST]
    assert experts_for_member("infra") == [Expert.INFRA]
    assert set(experts_for_member("all")) == set(Expert)


def test_cli_arguments() -> None:
    parser = build_parser()
    args = parser.parse_args(["--member", "test", "--from", "2026-03-01", "--to", "2026-03-31"])
    assert (args.member, args.start.isoformat(), args.end.isoformat()) == (
        "test",
        "2026-03-01",
        "2026-03-31",
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["--member", "nobody", "--from", "2026-03-01", "--to", "2026-03-02"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--member", "test", "--from", "March", "--to", "2026-03-02"])


def test_replay_import_path_never_loads_a_github_client() -> None:
    """Structural: importing the replay module must not import the GitHub client or live builder."""
    code = (
        "import sys, api.replay\n"
        "bad = [n for n in sys.modules if n.startswith(('shared.github', 'features.live'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""
