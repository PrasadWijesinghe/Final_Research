"""Tests for scripts/rerun_check.py. Recorded-style responses via respx; no real network."""

from __future__ import annotations

import csv
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import rerun_check
import respx
from collector.repo_config import RepoEntry
from collector.workflow_detection import load_detection_rules

from shared.github import GitHubClient, GitHubConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / "services" / "collector" / "tests" / "fixtures" / "workflows"
TODAY = date(2026, 10, 19)
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


def _run(created: str, conclusion: str = "success", attempt: int = 1) -> dict[str, Any]:
    return {
        "id": 1,
        "created_at": created,
        "conclusion": conclusion,
        "status": "completed",
        "run_attempt": attempt,
    }


class FakeGitHub:
    """Per-repo canned answers, dispatched on path and query. Records every request."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        full = "/".join(parts[1:3])
        spec = self.repos.get(full)
        if spec is None:
            return httpx.Response(404, json={"message": "Not Found"})
        tail = "/".join(parts[3:])
        q = request.url.params
        if tail == "":
            return httpx.Response(200, json=spec["repo"])
        if tail == "actions/workflows":
            return httpx.Response(
                200,
                json={
                    "total_count": len(spec["workflows"]),
                    "workflows": [{"path": p} for p in spec["workflows"]],
                },
            )
        if tail == "actions/runs":
            if spec.get("runs_error"):
                return httpx.Response(403, json={"message": "Actions are disabled"})
            if "created" in q and q["per_page"] == "100":
                runs = spec["sample"]
                return httpx.Response(
                    200, json={"total_count": spec["total_90d"], "workflow_runs": runs}
                )
            if q.get("status") == "failure":
                return httpx.Response(
                    200, json={"total_count": spec["failed_90d"], "workflow_runs": []}
                )
            page = int(q["page"])
            total = spec["total_all"]
            runs = [spec["oldest"]] if page == min(total, 1000) else []
            return httpx.Response(200, json={"total_count": total, "workflow_runs": runs})
        return httpx.Response(404)


def _repo_spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "repo": {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "archived": False,
            "license": {"spdx_id": "MIT"},
        },
        "sample": [
            _run("2026-10-18T10:00:00Z", "failure", 2),
            _run("2026-10-17T10:00:00Z", "failure"),
            _run("2026-10-16T10:00:00Z"),
            _run("2026-10-15T10:00:00Z"),
        ],
        "total_90d": 50,
        "failed_90d": 10,
        "total_all": 50,
        "oldest": _run("2026-07-25T08:00:00Z"),
        "workflows": [".github/workflows/test.yml", ".github/workflows/release.yml"],
    }
    spec.update(over)
    return spec


@pytest.fixture
def fake() -> Any:
    gh = FakeGitHub()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="api.github.com").mock(side_effect=gh.handler)
        files = {
            ".github/workflows/test.yml": WORKFLOWS / "mongoose_test.yml",
            ".github/workflows/release.yml": WORKFLOWS / "neg_release.yml",
        }

        def raw(request: httpx.Request) -> httpx.Response:
            gh.requests.append(request)
            for path, src in files.items():
                if request.url.path.endswith(path):
                    return httpx.Response(200, content=src.read_bytes())
            return httpx.Response(404)

        router.route(host="raw.githubusercontent.com").mock(side_effect=raw)
        yield gh


def _client() -> GitHubClient:
    # Obviously fake token; sleep is a no-op so retries would run instantly.
    return GitHubClient(token="fake-token-not-real", config=GitHubConfig(), sleep=lambda s: None)


def _check(name: str = "acme/widgets", licence: str = "MIT", **kw: Any) -> Any:
    entry = RepoEntry(full_name=name, licence=licence, **kw)
    with _client() as client:
        return rerun_check.check_repo(client, entry, "test", TODAY, load_detection_rules())


def test_happy_path_numbers_and_five_calls(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec()
    r = _check()
    assert r.error is None
    assert (r.runs_90d, r.failed_90d) == (50, 10)
    assert r.failure_rate == pytest.approx(0.2)
    assert (r.sample_size, r.sample_failure_rate, r.sample_rerun_share) == (4, 0.5, 0.25)
    assert r.oldest_created is not None and r.oldest_created.date() == date(2026, 7, 25)
    assert r.retention_days(TODAY) == 86 and not r.oldest_is_capped
    assert r.workflows_total == 2
    assert r.relevant_workflows == [".github/workflows/test.yml"]
    assert r.licence_status == "match"
    assert r.api_calls == 5
    assert r.raw_calls == 2
    rest = [x for x in fake.requests if x.url.host == "api.github.com"]
    assert len(rest) == 5
    assert rest[1].url.params["created"] == ">=2026-07-21"
    assert rest[2].url.params["status"] == "failure" and rest[2].url.params["per_page"] == "1"
    assert (rest[3].url.params["per_page"], rest[3].url.params["page"]) == ("1", "50")


def test_wrong_guess_for_oldest_costs_one_more_call(fake: FakeGitHub) -> None:
    # Runs older than the 90-day window still exist: 90d total 50, unfiltered total 60.
    fake.repos["acme/widgets"] = _repo_spec(total_all=60)
    r = _check()
    assert r.api_calls == 6
    assert r.oldest_created is not None


def test_result_cap_is_reported_honestly(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec(
        total_90d=2500, total_all=2500, oldest=_run("2026-09-30T00:00:00Z")
    )
    r = _check()
    assert r.oldest_is_capped
    cells = dict(zip(rerun_check.COLUMN_NAMES, rerun_check.report_cells(r, TODAY), strict=True))
    assert cells["Oldest run"] == "older than 2026-09-30"
    assert cells["Retention (days)"] == ">19"
    assert r.api_calls == 5  # the guess page (1000) was already the capped page


def test_licence_mismatch_and_unknown(fake: FakeGitHub) -> None:
    spec = _repo_spec()
    spec["repo"]["license"] = {"spdx_id": "Apache-2.0"}
    fake.repos["acme/widgets"] = spec
    assert _check().licence_status == "MISMATCH"
    spec["repo"]["license"] = {"spdx_id": "NOASSERTION"}
    assert _check().licence_status == "unknown"
    spec["repo"]["license"] = None
    assert _check().licence_status == "unknown"
    spec["repo"]["license"] = {"spdx_id": "mit"}
    assert _check().licence_status == "match"


def test_missing_repo_gives_error_row(fake: FakeGitHub) -> None:
    r = _check("gone/repo")
    assert r.error and "404" in r.error
    assert r.api_calls == 1
    cells = rerun_check.report_cells(r, TODAY)
    assert cells[-1].startswith("ERROR:")


def test_actions_disabled_keeps_licence_and_reports_error(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec(runs_error=True)
    r = _check()
    assert r.error and "Actions may be disabled" in r.error
    assert r.licence_github == "MIT"
    assert r.runs_90d is None


def test_repo_without_runs(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec(
        sample=[], total_90d=0, failed_90d=0, total_all=0, oldest=None
    )
    r = _check()
    assert r.error is None and r.runs_90d == 0
    assert r.failure_rate is None and r.oldest_created is None


def test_configured_workflow_missing_is_noted(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec()
    r = _check(workflows=[".github/workflows/nope.yml"])
    assert "configured workflow not found" in rerun_check.report_cells(r, TODAY)[-1]


# ---- end to end through main() ------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config"
    (cfg / "repos").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "config" / "workflow_detection.yaml", cfg)
    (cfg / "repos" / "test.yaml").write_text(
        "member: test\n"
        "repos:\n"
        "  - {full_name: acme/widgets, licence: MIT}\n"
        "  - {full_name: gone/repo, licence: MIT}\n"
        "  - {full_name: acme/off, licence: MIT, enabled: false}\n",
        encoding="utf-8",
    )
    return cfg


def test_main_writes_md_and_csv_and_continues_after_404(
    fake: FakeGitHub, config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.repos["acme/widgets"] = _repo_spec()
    out = tmp_path / "reports"
    with _client() as client:
        code = rerun_check.main(
            ["--member", "test", "--config-dir", str(config_dir), "--out-dir", str(out)],
            client=client,
            today=TODAY,
        )
    assert code == 0
    md = out / "rerun_check-test-2026-10-19.md"
    csv_path = out / "rerun_check-test-2026-10-19.csv"
    assert str(md) in capsys.readouterr().out
    text = md.read_text(encoding="utf-8")
    assert "acme/widgets" in text and "gone/repo" in text and "acme/off" not in text
    assert "skipped as disabled: 1" in text
    assert "## Legend" in text
    for name in rerun_check.COLUMN_NAMES:  # every column is explained in the legend
        assert f"| {name} |" in text
    rows = list(csv.reader(csv_path.open(encoding="utf-8", newline="")))
    assert rows[0] == rerun_check.COLUMN_NAMES
    assert len(rows) == 3
    by_repo = {r[0]: r for r in rows[1:]}
    assert by_repo["acme/widgets"][-3] == "5"  # API calls column
    assert by_repo["gone/repo"][-1].startswith("ERROR:")


def test_main_all_includes_disabled(fake: FakeGitHub, config_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "reports"
    with _client() as client:
        rerun_check.main(
            ["--member", "test", "--all", "--config-dir", str(config_dir), "--out-dir", str(out)],
            client=client,
            today=TODAY,
        )
    assert "acme/off" in (out / "rerun_check-test-2026-10-19.md").read_text(encoding="utf-8")


def test_main_without_token_exits_2(
    monkeypatch: pytest.MonkeyPatch, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert rerun_check.main(["--member", "test", "--config-dir", str(config_dir)]) == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err
