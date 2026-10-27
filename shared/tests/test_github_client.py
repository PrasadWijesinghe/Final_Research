"""GitHub client tests: recorded-style responses via respx, injected clock and sleep."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from shared.github import (
    GitHubClient,
    GitHubConfig,
    GitHubError,
    GitHubNotFoundError,
    GitHubResponseTooLargeError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"
API = "https://api.github.com"
RUNS = f"{API}/repos/acme/widgets/actions/runs"
RUNS_PATH = "/repos/acme/widgets/actions/runs"
FAKE_TOKEN = "fake-token-for-tests-only"
NOW = 1_790_000_000.0


def _run(run_id: int) -> dict[str, Any]:
    run: dict[str, Any] = json.loads((FIXTURES / "workflow_run.json").read_text())
    run["id"] = run_id
    return run


def _rl(remaining: int = 4000, reset: float = NOW + 3600) -> dict[str, str]:
    return {"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(int(reset))}


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def client(sleeper: Sleeper) -> GitHubClient:
    return GitHubClient(token=FAKE_TOKEN, config=GitHubConfig(), clock=lambda: NOW, sleep=sleeper)


def test_sends_token_and_counts_calls(client: GitHubClient) -> None:
    body = json.loads((FIXTURES / "rate_limit.json").read_text())
    with respx.mock:
        route = respx.get(f"{API}/rate_limit").respond(200, json=body, headers=_rl())
        result = client.get_rate_limit()
    assert result["rate"]["limit"] == 5000
    assert route.calls.last.request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert client.api_calls == 1
    assert client.raw_calls == 0


def test_pagination_follows_link_header(client: GitHubClient) -> None:
    page2 = f"{RUNS}?per_page=100&page=2"
    with respx.mock:
        # page 2 is registered first: respx params match by subset, so order matters
        respx.get(page2).respond(
            200, json={"total_count": 3, "workflow_runs": [_run(3)]}, headers=_rl()
        )
        respx.get(RUNS, params={"per_page": "100"}).respond(
            200,
            json={"total_count": 3, "workflow_runs": [_run(1), _run(2)]},
            headers={**_rl(), "link": f'<{page2}>; rel="next", <{page2}>; rel="last"'},
        )
        items = client.paginate(RUNS_PATH, items_key="workflow_runs")
        ids = [r["id"] for r in items]
    assert ids == [1, 2, 3]
    assert client.api_calls == 2


def test_pagination_plain_list(client: GitHubClient) -> None:
    with respx.mock:
        respx.get(f"{API}/repos/acme/widgets/commits").respond(200, json=[{"sha": "a"}])
        assert list(client.paginate("/repos/acme/widgets/commits")) == [{"sha": "a"}]


def test_sleeps_until_reset_when_remaining_low(client: GitHubClient, sleeper: Sleeper) -> None:
    with respx.mock:
        respx.get(f"{API}/a").respond(200, json={}, headers=_rl(remaining=150, reset=NOW + 120))
        respx.get(f"{API}/b").respond(200, json={}, headers=_rl(remaining=4999))
        client.get_json("/a")
        assert sleeper.calls == []  # no sleep after the last call, only before the next
        client.get_json("/b")
        client.get_json("/b")
    assert sleeper.calls == [121.0]


def test_no_sleep_when_remaining_at_threshold(client: GitHubClient, sleeper: Sleeper) -> None:
    with respx.mock:
        respx.get(f"{API}/a").respond(200, json={}, headers=_rl(remaining=200))
        client.get_json("/a")
        client.get_json("/a")
    assert sleeper.calls == []


def test_threshold_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_RATE_LIMIT_THRESHOLD", "50")
    assert GitHubConfig.from_env().rate_limit_threshold == 50


def test_retry_after_is_honoured(client: GitHubClient, sleeper: Sleeper) -> None:
    with respx.mock:
        respx.get(f"{API}/a").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "17"}, json={"message": "slow down"}),
                httpx.Response(403, headers={"retry-after": "5"}, json={"message": "limit"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert client.get_json("/a") == {"ok": True}
    assert sleeper.calls == [17.0, 5.0]
    assert client.api_calls == 3


def test_primary_limit_403_waits_for_reset(client: GitHubClient, sleeper: Sleeper) -> None:
    with respx.mock:
        respx.get(f"{API}/a").mock(
            side_effect=[
                httpx.Response(
                    403,
                    headers=_rl(remaining=0, reset=NOW + 30),
                    json={"message": "API rate limit exceeded for user ID 1."},
                ),
                httpx.Response(200, json={}),
            ]
        )
        client.get_json("/a")
    assert sleeper.calls == [31.0]


def test_secondary_rate_limit_backs_off(client: GitHubClient, sleeper: Sleeper) -> None:
    secondary = {"message": "You have exceeded a secondary rate limit. Please wait."}
    with respx.mock:
        respx.get(f"{API}/a").mock(
            side_effect=[httpx.Response(403, json=secondary), httpx.Response(200, json={})]
        )
        client.get_json("/a")
    assert sleeper.calls == [60.0]


def test_plain_403_is_an_error_without_retry(client: GitHubClient, sleeper: Sleeper) -> None:
    with respx.mock:
        respx.get(f"{API}/a").respond(403, json={"message": "Resource not accessible"})
        with pytest.raises(GitHubError) as err:
            client.get_json("/a")
    assert err.value.status_code == 403
    assert sleeper.calls == []


def test_5xx_exponential_backoff_with_cap(sleeper: Sleeper) -> None:
    cfg = GitHubConfig(max_retries=5, backoff_cap=5.0)
    client = GitHubClient(token=FAKE_TOKEN, config=cfg, clock=lambda: NOW, sleep=sleeper)
    with respx.mock:
        respx.get(f"{API}/a").mock(
            side_effect=[httpx.Response(502)] * 4 + [httpx.Response(200, json={"ok": 1})]
        )
        assert client.get_json("/a") == {"ok": 1}
    assert sleeper.calls == [1.0, 2.0, 4.0, 5.0]


def test_5xx_gives_up_after_max_retries(sleeper: Sleeper) -> None:
    cfg = GitHubConfig(max_retries=2)
    client = GitHubClient(token=FAKE_TOKEN, config=cfg, clock=lambda: NOW, sleep=sleeper)
    with respx.mock:
        route = respx.get(f"{API}/a").respond(503)
        with pytest.raises(GitHubError) as err:
            client.get_json("/a")
    assert err.value.status_code == 503
    assert route.call_count == 3
    assert sleeper.calls == [1.0, 2.0]


def test_transport_error_is_retried(client: GitHubClient, sleeper: Sleeper) -> None:
    with respx.mock:
        respx.get(f"{API}/a").mock(
            side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"ok": 1})]
        )
        assert client.get_json("/a") == {"ok": 1}
    assert sleeper.calls == [1.0]


def test_404_raises_not_found(client: GitHubClient) -> None:
    with respx.mock:
        respx.get(f"{API}/repos/x/y").respond(404, json={"message": "Not Found"})
        with pytest.raises(GitHubNotFoundError):
            client.get_json("/repos/x/y")


def test_get_bytes_follows_redirect_without_leaking_token(client: GitHubClient) -> None:
    blob = "https://pipelinesghubeus1.actions.githubusercontent.com/abc/log.txt"
    with respx.mock:
        respx.get(f"{API}/repos/acme/widgets/actions/jobs/7/logs").respond(
            302, headers={"location": blob}
        )
        blob_route = respx.get(blob).respond(200, content=b"line 1\nline 2\n")
        body = client.get_bytes("/repos/acme/widgets/actions/jobs/7/logs")
    assert body == b"line 1\nline 2\n"
    assert "authorization" not in blob_route.calls.last.request.headers


def test_get_bytes_stops_before_reading_body_over_limit(client: GitHubClient) -> None:
    with respx.mock:
        route = respx.get(f"{API}/large-log").respond(
            200,
            content=b"this body exceeds the configured limit",
            headers={"content-length": "38"},
        )
        with pytest.raises(GitHubResponseTooLargeError) as err:
            client.get_bytes("/large-log", max_bytes=8)
    assert err.value.max_bytes == 8
    assert err.value.content_length == 38
    assert route.call_count == 1


def test_get_bytes_limited_accepts_body_at_limit(client: GitHubClient) -> None:
    with respx.mock:
        respx.get(f"{API}/small-log").respond(200, content=b"12345678")
        assert client.get_bytes("/small-log", max_bytes=8) == b"12345678"


def test_get_bytes_limited_stops_streaming_when_no_length_is_available() -> None:
    class TrackingStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.yielded = 0

        def __iter__(self) -> Any:
            for chunk in (b"1234", b"5678", b"9", b"unused"):
                self.yielded += 1
                yield chunk

        def close(self) -> None:
            pass

    stream = TrackingStream()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=stream, headers={"content-type": "text/plain"})
    )
    client = GitHubClient(config=GitHubConfig(max_retries=0), transport=transport)
    with pytest.raises(GitHubResponseTooLargeError):
        client.get_bytes("/large-log", max_bytes=8)

    assert stream.yielded == 3
    client.close()


def test_get_bytes_rejects_nonpositive_limit(client: GitHubClient) -> None:
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        client.get_bytes("/small-log", max_bytes=0)


def test_window_splitting(client: GitHubClient) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 9, 3, tzinfo=UTC)  # 48 h; splits at 2026-09-02T00:00:00Z
    full = "2026-09-01T00:00:00Z..2026-09-03T00:00:00Z"
    left = "2026-09-01T00:00:00Z..2026-09-02T00:00:00Z"
    right = "2026-09-02T00:00:01Z..2026-09-03T00:00:00Z"
    with respx.mock:
        respx.get(RUNS, params={"per_page": "100", "created": full}).respond(
            200, json={"total_count": 1000, "workflow_runs": [_run(i) for i in range(100)]}
        )
        respx.get(RUNS, params={"per_page": "100", "created": left}).respond(
            200, json={"total_count": 2, "workflow_runs": [_run(1), _run(2)]}
        )
        respx.get(RUNS, params={"per_page": "100", "created": right}).respond(
            200, json={"total_count": 1, "workflow_runs": [_run(3)]}
        )
        ids = [r["id"] for r in client.iter_workflow_runs("acme/widgets", start, end)]
    assert ids == [1, 2, 3]
    assert client.api_calls == 3


def test_window_splitting_stops_at_minimum(
    sleeper: Sleeper, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = GitHubConfig(min_window=timedelta(hours=12))
    client = GitHubClient(token=FAKE_TOKEN, config=cfg, clock=lambda: NOW, sleep=sleeper)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 9, 2, tzinfo=UTC)  # 24 h -> two 12 h halves, which are not split again
    with respx.mock:
        route = respx.get(RUNS).mock(
            side_effect=[
                httpx.Response(200, json={"total_count": 1500, "workflow_runs": []}),
                httpx.Response(200, json={"total_count": 1200, "workflow_runs": [_run(1)]}),
                httpx.Response(200, json={"total_count": 1100, "workflow_runs": [_run(2)]}),
            ]
        )
        with caplog.at_level(logging.WARNING):
            ids = [r["id"] for r in client.iter_workflow_runs("acme/widgets", start, end)]
    assert ids == [1, 2]
    assert route.call_count == 3
    assert "truncated" in caplog.text


def test_window_pages_within_a_window_and_passes_params(client: GitHubClient) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 9, 2, tzinfo=UTC)
    page2 = f"{RUNS}?page=2"
    first = {
        "per_page": "100",
        "status": "failure",
        "created": "2026-09-01T00:00:00Z..2026-09-02T00:00:00Z",
    }
    with respx.mock:
        respx.get(RUNS, params=first).respond(
            200,
            json={"total_count": 2, "workflow_runs": [_run(1)]},
            headers={"link": f'<{page2}>; rel="next"'},
        )
        respx.get(page2).respond(200, json={"total_count": 2, "workflow_runs": [_run(2)]})
        runs = client.iter_workflow_runs("acme/widgets", start, end, {"status": "failure"})
        ids = [r["id"] for r in runs]
    assert ids == [1, 2]


def test_window_requires_aware_datetimes(client: GitHubClient) -> None:
    with pytest.raises(ValueError):
        list(client.iter_workflow_runs("a/b", datetime(2026, 1, 1), datetime(2026, 1, 2)))


RAW = "https://raw.githubusercontent.com/acme/widgets/3f1c9d2/package.json"


def test_raw_fetch_sends_token_and_counts_separately(client: GitHubClient) -> None:
    with respx.mock:
        route = respx.get(RAW).respond(200, content=b'{"name": "widgets"}')
        body = client.fetch_raw_file("acme/widgets", "3f1c9d2", "package.json")
    assert body == b'{"name": "widgets"}'
    assert route.calls.last.request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert (client.api_calls, client.raw_calls) == (0, 1)


def test_raw_fetch_404_returns_none(client: GitHubClient) -> None:
    with respx.mock:
        respx.get(RAW).respond(404, content=b"404: Not Found")
        assert client.fetch_raw_file("acme/widgets", "3f1c9d2", "package.json") is None


def test_raw_fetch_backs_off_on_429(client: GitHubClient, sleeper: Sleeper) -> None:
    with respx.mock:
        respx.get(RAW).mock(side_effect=[httpx.Response(429), httpx.Response(200, content=b"ok")])
        assert client.fetch_raw_file("acme/widgets", "3f1c9d2", "/package.json") == b"ok"
    assert sleeper.calls == [1.0]


def test_token_never_logged(client: GitHubClient, caplog: pytest.LogCaptureFixture) -> None:
    with respx.mock, caplog.at_level(logging.DEBUG):
        respx.get(f"{API}/a").mock(side_effect=[httpx.Response(502), httpx.Response(200, json={})])
        client.get_json("/a")
    assert caplog.records
    assert FAKE_TOKEN not in caplog.text
