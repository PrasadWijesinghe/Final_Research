from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import ctx_scenario as sc
import httpx
import pytest
import respx
from features.live import build_live_context
from features.pre_push import PrePushFile, PrePushPayload, PrePushWorkflow, build_pre_push_context
from features.replay import (
    CommitFileRow,
    InMemoryHistory,
    ReplayDataError,
    RunRow,
    build_replay_context,
)
from pydantic import ValidationError

from shared.contracts import CommitContext, FileTag
from shared.github.client import GitHubClient, GitHubConfig

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
PUSHED_AT = datetime(2026, 9, 1, 12, 5, tzinfo=UTC)
# context_id is random per context; event and history_cutoff are meant to differ.
SKIP = {"context_id", "event", "history_cutoff"}
FAKE_TOKEN = "ghp_" + "a1B2c3D4e5" * 3 + "a1B2c3"


@pytest.fixture(autouse=True)
def identity_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDENTITY_SALT", "fake-salt-for-tests")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/repos/{sc.REPO}/commits/{sc.HEAD}").respond(json=sc.commit_json())
        router.get(f"{API}/repos/{sc.REPO}/contents/.github/workflows").respond(
            json=[
                {"type": "file", "name": "ci.yml", "path": sc.CI},
                {"type": "file", "name": "release.yml", "path": sc.RELEASE},
                {"type": "file", "name": "README.md", "path": ".github/workflows/README.md"},
            ]
        )
        for path, text in sc.raw_contents().items():
            router.get(f"{RAW}/{sc.REPO}/{sc.HEAD}/{path}").respond(text=text)
        yield router


def live(**kw: Any) -> CommitContext:
    args: dict[str, Any] = {
        "repo": sc.REPO,
        "head_sha": sc.HEAD,
        "event": "push",
        "branch": sc.BRANCH,
        "history_cutoff": PUSHED_AT,
    }
    client = GitHubClient(token="fake-token", config=GitHubConfig(max_retries=0))
    return build_live_context(client, **{**args, **kw})


def replay(history: InMemoryHistory | None = None, **kw: Any) -> CommitContext:
    return build_replay_context(
        history or sc.history(), repo=sc.REPO, run_id=sc.RUN_ID, attempt=1, **kw
    )


def comparable(ctx: CommitContext) -> dict[str, Any]:
    return ctx.model_dump(mode="json", exclude=SKIP)


# ---- live vs replay (the P2.3 acceptance criterion) -----------------------------------------


def test_live_and_replay_build_equal_contexts(api: respx.MockRouter) -> None:
    paths = [sc.CI, sc.RELEASE]
    a = live(workflow_paths=paths)
    b = replay(workflow_paths=paths)
    assert comparable(a) == comparable(b)
    assert a.event == "push" and b.event == "replay"
    assert a.history_cutoff == PUSHED_AT and b.history_cutoff == sc.RUN_CREATED
    # the interesting parts are really there
    assert a.base_sha == sc.PARENT and a.author_hash == sc.author_hash()
    assert [f.path for f in a.changed_files] == sorted(sc.FILES)
    assert {s.path for s in a.snapshots} == {"package.json", "vite.config.ts", sc.CI}
    assert {j.job_key for j in a.workflow_jobs} == {"test", "publish"}
    test_job = next(j for j in a.workflow_jobs if j.job_key == "test")
    assert test_job.node_versions == ["18", "20"]


def test_default_workflow_discovery(api: respx.MockRouter) -> None:
    # Live lists the folder; replay uses the workflows of runs created before the cutoff, so the
    # later run (release.yml) is not visible. Changed workflow files are parsed in both.
    assert {j.job_key for j in live().workflow_jobs} == {"test", "publish"}
    assert {j.job_key for j in replay().workflow_jobs} == {"test"}


def test_vite_config_with_test_block_is_test_config(api: respx.MockRouter) -> None:
    for ctx in (live(), replay()):
        vite = next(f for f in ctx.changed_files if f.path == "vite.config.ts")
        assert FileTag.TEST_CONFIG in vite.tags and FileTag.BUNDLER_CONFIG in vite.tags


def test_lockfile_patch_dropped_and_not_snapshotted(api: respx.MockRouter) -> None:
    for ctx in (live(), replay()):
        lock = next(f for f in ctx.changed_files if f.path == "package-lock.json")
        assert lock.patch is None and FileTag.LOCKFILE in lock.tags
        assert all(s.path != "package-lock.json" for s in ctx.snapshots)
        assert not ctx.truncated


def test_live_only_fetches_config_files(api: respx.MockRouter) -> None:
    live()
    raw_paths = {c.request.url.path for c in api.calls if c.request.url.host.startswith("raw")}
    assert f"/{sc.REPO}/{sc.HEAD}/package.json" in raw_paths
    assert f"/{sc.REPO}/{sc.HEAD}/src/index.ts" not in raw_paths  # source: the patch is enough
    assert f"/{sc.REPO}/{sc.HEAD}/old.js" not in raw_paths  # removed
    assert f"/{sc.REPO}/{sc.HEAD}/package-lock.json" not in raw_paths  # lockfile


def test_live_secrets_in_patch_are_redacted(api: respx.MockRouter) -> None:
    body = sc.commit_json()
    body["files"][0]["patch"] = f"+TOKEN={FAKE_TOKEN}"
    api.get(f"{API}/repos/{sc.REPO}/commits/{sc.HEAD}").respond(json=body)
    assert FAKE_TOKEN not in live().model_dump_json()


def test_live_pull_request_uses_compare_diff(api: respx.MockRouter) -> None:
    base = "d" * 40
    api.get(f"{API}/repos/{sc.REPO}/compare/{base}...{sc.HEAD}").respond(
        json={
            "files": [{"filename": "src/pr.ts", "status": "added", "additions": 1, "deletions": 0}]
        }
    )
    ctx = live(event="pull_request", pr_number=12, base_sha=base)
    assert ctx.pr_number == 12 and ctx.base_sha == base
    assert [f.path for f in ctx.changed_files] == ["src/pr.ts"]
    assert ctx.changed_files[0].patch is None


def test_live_follows_file_pages(api: respx.MockRouter) -> None:
    page2 = {"files": [{"filename": "extra.ts", "status": "added", "additions": 1, "deletions": 0}]}
    link = f'<{API}/repos/{sc.REPO}/commits/{sc.HEAD}?page=2>; rel="next"'
    api.get(f"{API}/repos/{sc.REPO}/commits/{sc.HEAD}").mock(
        side_effect=[
            httpx.Response(200, json=sc.commit_json(), headers={"Link": link}),
            httpx.Response(200, json=page2),
        ]
    )
    assert "extra.ts" in {f.path for f in live().changed_files}


def test_live_skips_unreadable_workflow(api: respx.MockRouter) -> None:
    api.get(f"{RAW}/{sc.REPO}/{sc.HEAD}/{sc.RELEASE}").respond(text="a: [")
    assert {j.job_key for j in live().workflow_jobs} == {"test"}


def test_live_without_workflow_folder_still_parses_changed_workflows(
    api: respx.MockRouter,
) -> None:
    api.get(f"{API}/repos/{sc.REPO}/contents/.github/workflows").respond(404)
    assert {j.job_key for j in live().workflow_jobs} == {"test"}


def test_live_root_commit_has_no_base(api: respx.MockRouter) -> None:
    body = sc.commit_json()
    body["parents"] = []
    api.get(f"{API}/repos/{sc.REPO}/commits/{sc.HEAD}").respond(json=body)
    assert live().base_sha is None


# ---- replay ----------------------------------------------------------------------------------


def test_replay_cutoff_is_the_runs_created_at() -> None:
    ctx = replay()
    assert ctx.history_cutoff == sc.RUN_CREATED and ctx.event == "replay"
    assert ctx.committed_at == sc.COMMITTED_AT and ctx.branch == sc.BRANCH


def test_replay_ignores_runs_created_at_or_after_the_cutoff() -> None:
    h = sc.history()
    h.runs.append(
        (sc.REPO, RunRow(5, 1, sc.HEAD, sc.RUN_CREATED, sc.RELEASE, sc.BRANCH))  # same instant
    )
    assert {j.job_key for j in replay(h).workflow_jobs} == {"test"}
    # replaying the later run sees both workflows
    later = build_replay_context(h, repo=sc.REPO, run_id=sc.RUN_ID + 1, attempt=1)
    assert {j.job_key for j in later.workflow_jobs} == {"test", "publish"}
    assert later.history_cutoff == sc.RUN_CREATED + timedelta(hours=1)


def test_replay_marks_truncated_patches() -> None:
    h = sc.history()
    h.files[(sc.REPO, sc.HEAD)].append(
        CommitFileRow("big.js", "modified", 1, 1, None, patch_truncated=True)
    )
    assert replay(h).truncated is True
    assert replay().truncated is False


def test_replay_carries_pr_number_and_hash_fallback() -> None:
    h = sc.history()
    h.runs[0] = (
        sc.REPO,
        RunRow(sc.RUN_ID, 1, sc.HEAD, sc.RUN_CREATED, sc.CI, sc.BRANCH, "c" * 64, (9,)),
    )
    assert replay(h).pr_number == 9


@pytest.mark.parametrize("what", ["run", "commit", "branch", "author"])
def test_replay_missing_data_raises(what: str) -> None:
    h = sc.history()
    if what == "run":
        h.runs.clear()
    elif what == "commit":
        h.commits.clear()
    elif what == "branch":
        h.runs[0] = (sc.REPO, RunRow(sc.RUN_ID, 1, sc.HEAD, sc.RUN_CREATED))
    else:
        h.commits[(sc.REPO, sc.HEAD)] = type(h.commits[(sc.REPO, sc.HEAD)])(
            sc.HEAD, sc.COMMITTED_AT, None, ()
        )
        h.runs[0] = (sc.REPO, RunRow(sc.RUN_ID, 1, sc.HEAD, sc.RUN_CREATED, None, sc.BRANCH))
    with pytest.raises(ReplayDataError):
        replay(h)


def test_replay_never_touches_the_network() -> None:
    with respx.mock(assert_all_called=False) as router:
        replay()
        assert router.calls.call_count == 0


# ---- pre-push --------------------------------------------------------------------------------


def payload(**kw: Any) -> PrePushPayload:
    files = [
        PrePushFile(
            path=path,
            status=status,  # type: ignore[arg-type]
            additions=add,
            deletions=dele,
            patch=patch,
            content=content if path not in ("src/index.ts", "test/index.test.ts") else None,
        )
        for path, (status, add, dele, patch, content) in sc.FILES.items()
    ]
    base: dict[str, Any] = {
        "repo": sc.REPO,
        "head_sha": sc.HEAD,
        "base_sha": sc.PARENT,
        "branch": sc.BRANCH,
        "committed_at": sc.COMMITTED_AT,
        "files": files,
        "workflows": [PrePushWorkflow(path=sc.RELEASE, content=sc.RELEASE_YAML)],
    }
    return PrePushPayload(**{**base, **kw})


def test_pre_push_matches_live_apart_from_event(api: respx.MockRouter) -> None:
    a = live(workflow_paths=[sc.CI, sc.RELEASE])
    b = build_pre_push_context(payload(), author_hash=sc.author_hash(), history_cutoff=PUSHED_AT)
    assert comparable(a) == comparable(b)
    assert b.event == "pre_push" and b.pr_number is None


def test_pre_push_defaults_cutoff_to_now_and_redacts() -> None:
    files = [
        PrePushFile(
            path="src/a.ts",
            status="modified",
            additions=1,
            deletions=0,
            patch=f"+x = '{FAKE_TOKEN}'",
        )
    ]
    ctx = build_pre_push_context(payload(files=files, workflows=[]), author_hash="e" * 64)
    assert FAKE_TOKEN not in ctx.model_dump_json()
    assert abs((datetime.now(UTC) - ctx.history_cutoff).total_seconds()) < 60


def test_pre_push_payload_rejects_bad_input() -> None:
    with pytest.raises(ValidationError):
        payload(repo="no-slash")
    with pytest.raises(ValidationError):
        payload(head_sha="abc")
    with pytest.raises(ValidationError):
        payload(unexpected=1)
