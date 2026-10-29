"""Tests for scripts/build_screen.py. Canned GitHub answers via respx; no real network."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

import build_screen
import httpx
import pytest
import respx
from collector.repo_config import RepoEntry
from collector.workflow_detection import load_detection_rules

from shared.github import GitHubClient, GitHubConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / "services" / "collector" / "tests" / "fixtures" / "workflows"
TODAY = date(2026, 10, 22)  # the 90-day look-back is exactly 13 seven-day windows
CI = ".github/workflows/ci.yml"
RELEASE = ".github/workflows/release.yml"
STEPS = ["Run pnpm run typecheck", "Lint", "Run pnpm test", "Run npm ci"]


def _run(i: int) -> dict[str, Any]:
    """Sample run i: even runs are in the build workflow; i % 6 in (0, 3) failed."""
    return {
        "id": 1000 + i,
        "path": (CI if i % 2 == 0 else RELEASE) + "@refs/heads/main",
        "conclusion": "failure" if i % 6 in (0, 3) else "success",
    }


def _jobs(step: str) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = [
        {"name": "ok", "conclusion": "success", "steps": []},
        {
            "name": "quality",
            "conclusion": "failure",
            "steps": [
                {"name": "Run actions/checkout@v4", "conclusion": "success"},
                {"name": step, "conclusion": "failure"},
            ],
        },
    ]
    return {"total_count": len(jobs), "jobs": jobs}


class FakeGitHub:
    """Canned answers for acme/web: a build workflow (ci.yml) and a release workflow."""

    def __init__(self, weekly_runs: int = 60, weekly_failed: int = 24) -> None:
        self.weekly_runs, self.weekly_failed = weekly_runs, weekly_failed
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, q = request.url, request.url.params
        if url.host == "raw.githubusercontent.com":
            name = "build_monorepo.yml" if url.path.endswith("ci.yml") else "neg_release.yml"
            return httpx.Response(200, content=(WORKFLOWS / name).read_bytes())
        path = url.path
        if path == "/repos/acme/web":
            return httpx.Response(
                200, json={"default_branch": "main", "license": {"spdx_id": "Apache-2.0"}}
            )
        if path == "/repos/acme/web/actions/workflows":
            wfs = [
                {"id": 1, "path": CI, "state": "active"},
                {"id": 2, "path": RELEASE, "state": "active"},
                {"id": 3, "path": ".github/workflows/old.yml", "state": "disabled_manually"},
            ]
            return httpx.Response(200, json={"total_count": 3, "workflows": wfs})
        if path == "/repos/acme/web/actions/runs":
            if "created" in q:  # a 7-day count
                n = self.weekly_failed if q.get("status") == "failure" else self.weekly_runs
                return httpx.Response(200, json={"total_count": n, "workflow_runs": [{"id": 1}]})
            page = int(q.get("page", 1))
            ids = range(0, 100) if page == 1 else range(100, 120) if page == 2 else range(0)
            return httpx.Response(200, json={"workflow_runs": [_run(i) for i in ids]})
        if path.startswith("/repos/acme/web/actions/runs/") and path.endswith("/jobs"):
            k = (int(path.split("/")[-2]) - 1000) // 6
            return httpx.Response(200, json=_jobs(STEPS[k % len(STEPS)]))
        if path == "/repos/gone/repo":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500, json={"message": f"unexpected {path}"})


def _client() -> GitHubClient:
    return GitHubClient(token="t", config=GitHubConfig(max_retries=0), sleep=lambda _s: None)


def _screen(fake: FakeGitHub, name: str = "acme/web") -> build_screen.ScreenReport:
    entry = RepoEntry(full_name=name, licence="MIT")
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="api.github.com").mock(side_effect=fake.handler)
        mock.route(host="raw.githubusercontent.com").mock(side_effect=fake.handler)
        with _client() as gh:
            return build_screen.screen_repo(gh, entry, TODAY, load_detection_rules())


def test_estimates_from_window_counts_and_samples() -> None:
    r = _screen(FakeGitHub())
    assert r.build_workflows == 1
    assert (r.runs_90d, r.failed_90d, r.counts_capped) == (13 * 60, 13 * 24, False)
    assert (r.sample_runs, r.sample_build_runs) == (120, 60)
    assert (r.sample_failed, r.sample_build_failed) == (40, 20)
    assert r.build_run_share == 0.5
    assert r.est_build_runs == 390
    assert r.est_build_workflow_failures == 156  # 312 failures x 20/40 in build workflows
    assert (r.opened_failed_runs, r.opened_build_failures) == (12, 6)
    assert r.est_build_failures == 78
    assert r.est_pass1_calls == round(390 * build_screen.CALLS_PER_RUN)
    assert r.est_failed_log_hours == pytest.approx(156 * build_screen.SECONDS_PER_LOG / 3600)
    assert r.suggestion == "keep"
    assert r.licence_github == "Apache-2.0"
    # repo, workflows, two sample pages, 13 x 2 window counts, 12 job lists
    assert r.api_calls == 1 + 1 + 2 + 26 + 12


def test_counts_use_windows_never_one_big_filtered_query() -> None:
    fake = FakeGitHub()
    _screen(fake)
    counts = [req for req in fake.requests if "created" in req.url.params]
    assert len(counts) == 26
    assert all(".." in req.url.params["created"] for req in counts)
    assert not any("/actions/workflows/" in req.url.path for req in fake.requests)


@pytest.mark.parametrize(
    ("weekly_runs", "weekly_failed", "suggestion"),
    [
        (60, 6, "too quiet"),
        (2_500, 300, "expensive"),
        (2_400, 240, "expensive"),
        (2_000, 240, "keep"),
    ],
)
def test_suggestions(weekly_runs: int, weekly_failed: int, suggestion: str) -> None:
    assert _screen(FakeGitHub(weekly_runs, weekly_failed)).suggestion == suggestion


def test_capped_counts_are_shown_as_at_least() -> None:
    r = _screen(FakeGitHub(2_500, 300))
    cells = dict(zip(build_screen.COLUMNS, build_screen.report_cells(r), strict=True))
    assert r.counts_capped
    assert cells["runs 90d (all)"].startswith("≥")
    assert "very busy" in cells["notes"]


def test_missing_repo_is_recorded_not_raised() -> None:
    r = _screen(FakeGitHub(), "gone/repo")
    assert r.error is not None and "not found" in r.error
    assert r.suggestion == "check by hand"


def test_main_writes_markdown_and_csv(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "repos").mkdir(parents=True)
    (config / "repos" / "build.yaml").write_text(
        "member: build\nrepos:\n  - {full_name: acme/web, licence: MIT}\n"
        "  - {full_name: acme/old, licence: MIT, enabled: false}\n",
        encoding="utf-8",
    )
    (config / "workflow_detection.yaml").write_bytes(
        (REPO_ROOT / "config" / "workflow_detection.yaml").read_bytes()
    )
    fake = FakeGitHub()
    out = tmp_path / "reports"
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="api.github.com").mock(side_effect=fake.handler)
        mock.route(host="raw.githubusercontent.com").mock(side_effect=fake.handler)
        with _client() as gh:
            code = build_screen.main(
                ["--config-dir", str(config), "--out-dir", str(out)], client=gh, today=TODAY
            )
    assert code == 0
    md = (out / "build_screen-2026-10-22.md").read_text(encoding="utf-8")
    assert "| acme/web | yes | MIT | Apache-2.0 | 1 | 780 | 50% |" in md
    assert "licence differs" in md and "acme/old" not in md
    assert "Repos marked keep: 1." in md
    rows = list(csv.reader((out / "build_screen-2026-10-22.csv").open(encoding="utf-8")))
    assert rows[0][0] == "repo" and rows[1][0] == "acme/web"


def test_main_needs_a_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert build_screen.main(["--out-dir", str(tmp_path)]) == 2
