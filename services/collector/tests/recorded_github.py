"""A fake GitHub built from recorded-style JSON in `fixtures/github/` (served through respx).

The scenario is the repo `acme/widgets`, with these runs (see the fixture files):

- run 9001: relevant workflow, a re-run (2 attempts); attempt 1 failed.
  Jobs 5001-5004. Job 5003's log is missing (404) and 5004's has expired (410).
- run 9002: relevant, failed; its commit changes a lockfile and has a patch over 1 MB.
  Jobs 5005-5007; 5006 is cancelled (no log).
- run 9003: the `Release` workflow, which Test-member detection marks not relevant.
- run 9004: relevant, old (1 August), one job.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import respx

FIXTURES = Path(__file__).parent / "fixtures" / "github"
REPO = "acme/widgets"
REPO_ID = 424242
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
SHA_A, SHA_B, SHA_C = "a1" * 20, "b2" * 20, "c3" * 20
HEAD_SHA = "d4" * 20
FAKE_TOKEN = "fake-token-for-tests-only"
FAKE_SALT = "fake-salt-for-tests-only"
# Built by concatenation so this file does not look like a real secret to scanners.
LOG_SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PATCH_SECRET = "ghp_" + "Qw1Er2Ty3Ui4Op5As6Df7Gh8Jk9Lz0Xc1Vb2"
COMMIT_SECRET = "ghp_" + "Zy9x8W7v6U5t4S3r2Q1p0O9n8M7l6K5j4I3h"

BIG_PATCH_BYTES = 1_100_000
_RATE = {"x-ratelimit-remaining": "4900", "x-ratelimit-reset": "1790000000"}


def load_json(rel: str) -> Any:
    """Load a fixture JSON file."""
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


class SimulatedCrash(RuntimeError):
    """Raised on purpose in the middle of a backfill."""


class FakeGitHub:
    """Answers GitHub requests from fixtures and remembers every request it saw."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.log_status: dict[int, int] = {}  # job id -> HTTP status to answer with
        self.hide_attempts = False  # answer 404 to /attempts/{n}
        self.crash_on_log: set[int] = set()  # raise SimulatedCrash once for these job ids
        # run id -> newest attempt GitHub "has so far"; later attempts are hidden. Lets a test
        # backfill first and then lift the cap, as if someone had re-run an old run meanwhile.
        self.attempt_cap: dict[int, int] = {}
        self.fail_runs_listing = False  # answer 500 to the runs listing (a failing repo)
        self._api = f"/repos/{REPO}"

    # ---- plumbing ---------------------------------------------------------------------------

    def install(self, router: respx.MockRouter) -> FakeGitHub:
        """Route every GET to this fake; anything unknown fails the test loudly."""
        router.route(method="GET").mock(side_effect=self.handle)
        return self

    def count(self, fragment: str) -> int:
        """How many requests contained `fragment` (host plus path plus query)."""
        return sum(1 for r in self.requests if fragment in r)

    @staticmethod
    def _json(
        body: Any, status: int = 200, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return httpx.Response(status, json=body, headers={**_RATE, **(headers or {})})

    @staticmethod
    def _not_found() -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"}, headers=_RATE)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Dispatch one request."""
        url = request.url
        self.requests.append(
            f"{url.host}{url.path}" + (f"?{url.query.decode()}" if url.query else "")
        )
        path = unquote(url.path)
        if url.host == "raw.githubusercontent.com":
            return self._raw(path)
        if url.host == "pipelines.actions.githubusercontent.com":
            job = re.fullmatch(r"/log/(\d+)", path)
            return self._log_file(int(job.group(1))) if job else self._not_found()
        if url.host != "api.github.com":
            raise AssertionError(f"unexpected host {url.host}")
        return self._api_call(path, dict(url.params))

    def _raw(self, path: str) -> httpx.Response:
        match = re.fullmatch(rf"/{REPO}/[0-9a-f]{{40}}/(.+)", path)
        file = FIXTURES / "raw" / match.group(1) if match else None
        if file is None or not file.is_file():
            return httpx.Response(404, text="404: Not Found")
        return httpx.Response(200, content=file.read_bytes())

    def _log_file(self, job_id: int) -> httpx.Response:
        file = FIXTURES / "logs" / f"{job_id}.log"
        if not file.is_file():
            return self._not_found()
        content = file.read_bytes().replace(b"@@LOG_SECRET@@", LOG_SECRET.encode())
        return httpx.Response(200, content=content)

    def _api_call(self, path: str, params: dict[str, str]) -> httpx.Response:
        api = self._api
        if path == api:
            return self._json(load_json("repo.json"))
        if path == "/rate_limit":
            core = {"limit": 5000, "used": 100, "remaining": 4900, "reset": 1790000000}
            return self._json({"resources": {"core": core}, "rate": core})
        if path == f"{api}/branches/main":
            return self._json(load_json("branch_main.json"))
        if path == f"{api}/actions/workflows":
            return self._json(load_json("workflows.json"))
        if path == f"{api}/actions/runs":
            return self._runs(params)
        if m := re.fullmatch(rf"{api}/actions/runs/(\d+)/attempts/(\d+)", path):
            if self.hide_attempts:
                return self._not_found()
            return self._fixture(f"attempts/{m.group(1)}_{m.group(2)}.json")
        if m := re.fullmatch(rf"{api}/actions/runs/(\d+)/jobs", path):
            assert params.get("filter") == "all", "jobs must be requested with filter=all"
            return self._jobs(int(m.group(1)))
        if m := re.fullmatch(rf"{api}/actions/jobs/(\d+)/logs", path):
            return self._job_log(int(m.group(1)))
        if m := re.fullmatch(rf"{api}/commits/([0-9a-f]{{40}})", path):
            return self._commit(m.group(1))
        raise AssertionError(f"unexpected API request {path}")

    def _fixture(self, rel: str) -> httpx.Response:
        if not (FIXTURES / rel).is_file():
            return self._not_found()
        return self._json(load_json(rel))

    def _jobs(self, run_id: int) -> httpx.Response:
        rel = f"jobs/{run_id}.json"
        if not (FIXTURES / rel).is_file():
            return self._not_found()
        body = load_json(rel)
        cap = self.attempt_cap.get(run_id)
        if cap is not None:
            body["jobs"] = [j for j in body["jobs"] if int(j["run_attempt"]) <= cap]
            body["total_count"] = len(body["jobs"])
        return self._json(body)

    def _runs(self, params: dict[str, str]) -> httpx.Response:
        if self.fail_runs_listing:
            return self._json({"message": "Server Error"}, status=500)
        start_s, end_s = params["created"].split("..")
        start = datetime.fromisoformat(start_s)
        end = datetime.fromisoformat(end_s)
        runs = [
            r
            for r in load_json("runs.json")["workflow_runs"]
            if start <= datetime.fromisoformat(r["created_at"]) <= end
        ]
        for i, r in enumerate(runs):
            cap = self.attempt_cap.get(r["id"])
            if cap is not None and cap < int(r["run_attempt"]):
                runs[i] = load_json(f"attempts/{r['id']}_{cap}.json")  # the older state of the run
        return self._json({"total_count": len(runs), "workflow_runs": runs})

    def _job_log(self, job_id: int) -> httpx.Response:
        if job_id in self.crash_on_log:
            self.crash_on_log.discard(job_id)
            raise SimulatedCrash(f"simulated crash while fetching the log of job {job_id}")
        if job_id in self.log_status:
            return self._json({"message": "Forbidden"}, status=self.log_status[job_id])
        if job_id == 5004:
            return self._json({"message": "Logs expired"}, status=410)
        if job_id == 5001:  # real GitHub answers with a redirect to blob storage
            location = f"https://pipelines.actions.githubusercontent.com/log/{job_id}"
            return httpx.Response(302, headers={**_RATE, "location": location})
        return self._log_file(job_id)

    def _commit(self, sha: str) -> httpx.Response:
        rel = f"commits/{sha}.json"
        if not (FIXTURES / rel).is_file():
            return self._not_found()
        text = (FIXTURES / rel).read_text(encoding="utf-8")
        body = json.loads(text.replace("@@COMMIT_SECRET@@", COMMIT_SECRET))
        for f in body["files"]:
            if f["filename"] == "src/a.js":  # a secret inside a diff must not be stored
                f["patch"] += "+const token = '" + PATCH_SECRET + "'\n"
            if f.get("patch") == "@@BIG@@":
                f["patch"] = "@@ -1 +1,9000 @@\n" + "+x = 1;\n" * (BIG_PATCH_BYTES // 7)
        return self._json(body)


def file_checksums(root: Path) -> dict[str, str]:
    """sha256 of every file under `root`, keyed by relative POSIX path."""
    import hashlib

    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


Hook = Callable[[FakeGitHub], None]
