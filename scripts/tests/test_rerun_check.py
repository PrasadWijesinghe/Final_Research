"""Tests for scripts/rerun_check.py. Recorded-style responses via respx; no real network."""

from __future__ import annotations

import csv
import shutil
from datetime import UTC, date, datetime, timedelta
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


def _spread_runs() -> list[dict[str, Any]]:
    """50 runs spread over the 90 days, every second one failed, run 0 was re-run."""
    base = datetime(2026, 7, 22, 10, tzinfo=UTC)
    runs = []
    for i in range(50):
        when = base + timedelta(days=int(i * 1.7))
        runs.append(
            _run(
                when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "failure" if i % 2 == 1 else "success",
                2 if i == 0 else 1,
            )
        )
    return runs


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeGitHub:
    """Per-repo canned answers: a list of runs, filtered by `created` range and `status`."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def _runs_answer(self, spec: dict[str, Any], q: Any) -> httpx.Response:
        runs: list[dict[str, Any]] = sorted(
            spec["runs"], key=lambda r: r["created_at"], reverse=True
        )
        if "created" in q:
            lo, hi = (_parse(x) for x in q["created"].split(".."))
            runs = [r for r in runs if lo <= _parse(r["created_at"]) <= hi]
            if q.get("status") == "failure":
                runs = [r for r in runs if r["conclusion"] == "failure"]
                if spec.get("failure_overcount"):
                    return httpx.Response(
                        200, json={"total_count": len(runs) + 5, "workflow_runs": runs[:1]}
                    )
            elif spec.get("runs_undercount"):
                return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
            total = min(len(runs), spec.get("count_cap", 2500))
            if spec.get("capped_windows") and runs:
                total = 2500
            return httpx.Response(
                200, json={"total_count": total, "workflow_runs": runs[: int(q["per_page"])]}
            )
        total = spec.get("total_all", len(runs))
        if "page" in q:  # per_page=1: the page number is the run's position
            page = int(q["page"])
            picked = runs[page - 1 : page] if page <= len(runs) else [spec["oldest"]]
            return httpx.Response(200, json={"total_count": total, "workflow_runs": picked})
        return httpx.Response(
            200, json={"total_count": total, "workflow_runs": runs[: int(q["per_page"])]}
        )

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
            return self._runs_answer(spec, q)
        return httpx.Response(404)


def _repo_spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "repo": {
            "full_name": "acme/widgets",
            "default_branch": "main",
            "archived": False,
            "license": {"spdx_id": "MIT"},
        },
        "runs": _spread_runs(),
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
    # Obviously fake token; sleep is a no-op so retries run instantly.
    return GitHubClient(token="fake-token-not-real", config=GitHubConfig(), sleep=lambda s: None)


def _check(name: str = "acme/widgets", licence: str = "MIT", **kw: Any) -> Any:
    entry = RepoEntry(full_name=name, licence=licence, **kw)
    with _client() as client:
        return rerun_check.check_repo(client, entry, "test", TODAY, load_detection_rules())


def test_count_windows_cover_90_days_without_overlap() -> None:
    windows = rerun_check.count_windows(TODAY)
    assert len(windows) == 13
    assert windows[0][0] == datetime(2026, 7, 21, tzinfo=UTC)
    assert windows[-1][1] == datetime(2026, 10, 19, 23, 59, 59, tzinfo=UTC)
    for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
        assert start - end == timedelta(seconds=1)


def test_happy_path_numbers_and_budget(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec()
    r = _check()
    assert r.error is None
    assert (r.runs_90d, r.failed_90d) == (50, 25)
    assert not r.runs_capped and not r.failed_capped
    assert r.failure_rate == pytest.approx(25 / 50)
    # the sample is the newest 100 runs (here: all 50)
    assert r.sample_size == 50 and r.sample_rerun == 1
    assert r.oldest_created is not None and r.oldest_created.date() == date(2026, 7, 22)
    assert r.retention_days(TODAY) == 89 and not r.oldest_is_capped
    assert r.workflows_total == 2
    assert r.relevant_workflows == [".github/workflows/test.yml"]
    assert r.licence_status == "match"
    rest = [x for x in fake.requests if x.url.host == "api.github.com"]
    # repo + sample + 13 run windows + 13 failure windows + oldest + workflows
    assert r.api_calls == len(rest) == 30
    assert r.raw_calls == 2
    # never one big filtered query: every created filter is a closed 7-day range
    for req in rest:
        created = req.url.params.get("created")
        if created:
            lo, hi = (_parse(x) for x in created.split(".."))
            assert hi - lo < timedelta(days=7)


def test_oldest_uses_the_unfiltered_total_so_no_extra_call(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec()
    assert _check().api_calls == 30


def test_capped_counts_are_shown_as_at_least_and_give_no_rate(fake: FakeGitHub) -> None:
    fake.repos["acme/widgets"] = _repo_spec(
        capped_windows=True, total_all=2500, oldest=_run("2026-09-30T00:00:00Z")
    )
    r = _check()
    assert r.runs_capped and r.failed_capped
    assert r.failure_rate is None
    cells = dict(zip(rerun_check.COLUMN_NAMES, rerun_check.report_cells(r, TODAY), strict=True))
    assert cells["Runs 90d"].startswith("≥")
    assert cells["Failed 90d"].startswith("≥")
    assert cells["Failure rate 90d"] == ""
    assert "at least" in cells["Notes"]
    assert r.oldest_is_capped and cells["Oldest run"].startswith("older than")


def test_failed_is_never_more_than_runs(fake: FakeGitHub) -> None:
    # GitHub says 0 runs in every window but still counts failures (the real bug).
    fake.repos["acme/widgets"] = _repo_spec(runs_undercount=True, failure_overcount=True)
    r = _check()
    assert r.runs_90d is not None and r.failed_90d is not None
    assert r.failed_90d <= r.runs_90d
    assert r.runs_capped  # shown as "at least"
    assert r.failure_rate is None
    cells = dict(zip(rerun_check.COLUMN_NAMES, rerun_check.report_cells(r, TODAY), strict=True))
    assert cells["Runs 90d"].startswith("≥") and cells["Failure rate 90d"] == ""
    assert "more failed than total" in cells["Notes"]


def test_quiet_windows_are_verified_not_trusted(fake: FakeGitHub) -> None:
    # Only two runs, in different weeks: the other 11 empty windows are re-asked and split.
    runs = [_run("2026-07-22T10:00:00Z", "failure"), _run("2026-10-18T10:00:00Z")]
    fake.repos["acme/widgets"] = _repo_spec(runs=runs)
    r = _check()
    assert (r.runs_90d, r.failed_90d) == (2, 1)
    # empty windows cost 5 calls each (1 + 2 retries + 2 halves) instead of 1
    assert r.api_calls > 30


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
    fake.repos["acme/widgets"] = _repo_spec(runs=[])
    r = _check()
    assert r.error is None and r.runs_90d == 0 and r.failed_90d == 0
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
    assert by_repo["acme/widgets"][-3] == "30"  # API calls column
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
