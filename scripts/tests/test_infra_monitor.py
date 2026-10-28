"""Real Git objects and remote pushes exercise the complete extraction and model path."""

import subprocess
from pathlib import Path

import infra_monitor
import pytest
from fastapi.testclient import TestClient
from infra_expert import api
from infra_expert.features import raw_features_from_context
from infra_pre_push import ZERO_SHA, build_context


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit(repo: Path, name: str, content: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-m", "test change")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path):
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Infra Test")
    workflow = """name: CI
on: [push]
permissions:
  contents: read
jobs:
  build:
    runs-on: self-hosted
    timeout-minutes: 5
    steps:
      - run: npm run build
"""
    initial = commit(repo, ".github/workflows/ci.yml", workflow)
    return repo, initial


def test_initial_push_and_unchanged_workflow_inputs(repository):
    repo, initial = repository
    first = build_context(
        cwd=repo,
        local_ref="refs/heads/main",
        head_sha=initial,
        remote_sha=ZERO_SHA,
        remote_url="https://github.com/test/app",
    )
    assert first.base_sha is None
    assert len(first.changed_files) == 1
    head = commit(repo, "src/hello.jsx", "export default () => 'hello'\n")
    context = build_context(
        cwd=repo,
        local_ref="refs/heads/main",
        head_sha=head,
        remote_sha=initial,
        remote_url="https://github.com/test/app",
    )
    raw = raw_features_from_context(context)
    assert raw["Uses_Self_Hosted_Runner"] is True
    assert raw["Timeout_Defined"] is True
    assert raw["Permissions_Defined"] is True
    assert raw["Workflow_Changed"] is False
    assert raw["Configured_Job_Count"] == 1
    assert context.snapshots[0].changed is False


def test_new_branch_includes_all_commits_and_rename_counts(repository):
    repo, _ = repository
    commit(repo, "src/old name.jsx", "one\ntwo\nthree\nfour\nfive\n")
    before = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "src/old name.jsx", "src/new name.jsx")
    head = commit(repo, "src/new name.jsx", "one\ntwo\nthree\nfour\nfive\nsix\n")
    context = build_context(
        cwd=repo,
        local_ref="refs/heads/new",
        head_sha=head,
        remote_sha=ZERO_SHA,
        remote_url="https://github.com/test/app",
    )
    assert len(context.changed_files) == 2
    renamed = build_context(
        cwd=repo,
        local_ref="refs/heads/main",
        head_sha=head,
        remote_sha=before,
        remote_url="https://github.com/test/app",
    )
    assert renamed.changed_files[0].previous_path == "src/old name.jsx"
    assert renamed.changed_files[0].additions == 1


def test_real_remote_push_to_trained_model_with_retry_and_restart(
    repository, tmp_path, monkeypatch
):
    repo, initial = repository
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "push", str(remote), "main")
    api._push_history.clear()
    api._latest_push = None
    client = TestClient(api.app)
    contexts = []

    def deliver(context, url, timeout):
        contexts.append(context)
        response = client.post("/monitor/predict", json=context.model_dump(mode="json"))
        assert response.status_code == 200
        return response.json()[0]

    monkeypatch.setattr(infra_monitor, "request_prediction", deliver)
    watcher = infra_monitor.RemoteMonitor(str(remote), "main", tmp_path / "cache")
    assert watcher.poll("unused") == initial
    assert watcher.poll("unused") is None
    head = commit(repo, "src/app.jsx", "export default () => 'new push'\n")
    git(repo, "push", str(remote), "main")

    def offline(*args):
        raise OSError("offline")

    monkeypatch.setattr(infra_monitor, "request_prediction", offline)
    with pytest.raises(OSError):
        watcher.poll("unused")
    assert watcher.previous == initial
    monkeypatch.setattr(infra_monitor, "request_prediction", deliver)
    assert watcher.poll("unused") == head
    latest = client.get("/demo/latest").json()
    assert latest["head_sha"] == head
    assert latest["source"] == "push"
    assert latest["prediction"]["model_version"] == "v6-public-dataset-research"
    assert 0 <= latest["prediction"]["probability"] <= 1
    assert contexts[-1].workflow_jobs[0].runs_on == ["self-hosted"]
    assert len(client.get("/monitor/history").json()) == 2
    restarted = infra_monitor.RemoteMonitor(str(remote), "main", tmp_path / "cache")
    assert restarted.poll("unused") == head
    assert len(client.get("/monitor/history").json()) == 2
