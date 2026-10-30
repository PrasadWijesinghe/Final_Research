"""GitHub REST client: token auth, pagination, rate limits, retries and date-window splitting."""

from __future__ import annotations

import io
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"
RAW_URL = "https://raw.githubusercontent.com"
RESULT_CAP = 1000  # the runs listing stops returning results past 1,000
TOTAL_COUNT_CAP = 2500  # a `total_count` of 2500 means "2500 or more", never exactly 2500


class GitHubError(Exception):
    """A GitHub request failed for good (after retries, or a non-retryable status)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_server_error(self) -> bool:
        """True for a 5xx answer (GitHub is failing on this request); False for network errors."""
        return self.status_code is not None and self.status_code >= 500


class GitHubNotFoundError(GitHubError):
    """The resource does not exist (404), or is not visible to the token."""


class GitHubTooLargeError(GitHubError):
    """A body was larger than the caller's `max_bytes`; it was not read into memory."""

    def __init__(
        self, message: str, *, max_bytes: int | None = None, content_length: int | None = None
    ) -> None:
        super().__init__(message)
        self.max_bytes = max_bytes
        self.content_length = content_length


# Older name used by the Dependency research scripts on main; same error.
GitHubResponseTooLargeError = GitHubTooLargeError


@dataclass(frozen=True)
class RunCount:
    """Number of runs in a window. `capped` means "at least `count`" (GitHub stops counting)."""

    count: int
    capped: bool = False


@dataclass(frozen=True)
class GitHubConfig:
    """Tunable client behaviour. Values come from the environment via `from_env`."""

    # DEFAULT: 200 is the threshold named in docs/DATA.md section 3.3.
    rate_limit_threshold: int = 200
    # DEFAULT: 5 retries, 1 s doubling to a 60 s cap; bounded in time, no jitter (deterministic).
    max_retries: int = 5
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    # DEFAULT: a secondary-rate-limit 403 without retry-after waits at least 60 s (GitHub's advice).
    secondary_backoff: float = 60.0
    # DEFAULT: stop halving date windows at one hour; below that we accept truncation and warn.
    min_window: timedelta = timedelta(hours=1)
    # DEFAULT: an empty run window is re-asked twice (1 s, 2 s backoff) before it is cross-checked;
    # GitHub sometimes answers slow filtered queries with total_count 0 instead of an error.
    empty_retries: int = 2
    # DEFAULT: 30 s request timeout.
    timeout: float = 30.0
    # DEFAULT: after a 5xx on a listing, ask again with 30, then 10 items per page (big runs make
    # GitHub time out on 100 jobs per page).
    fallback_page_sizes: tuple[int, ...] = (30, 10)
    base_url: str = API_URL
    raw_url: str = RAW_URL

    @classmethod
    def from_env(cls) -> GitHubConfig:
        """Build a config, reading GITHUB_RATE_LIMIT_THRESHOLD and GITHUB_MIN_WINDOW_HOURS."""
        defaults = cls()
        threshold = int(
            os.environ.get("GITHUB_RATE_LIMIT_THRESHOLD") or defaults.rate_limit_threshold
        )
        hours = float(os.environ.get("GITHUB_MIN_WINDOW_HOURS") or 0)
        window = timedelta(hours=hours) if hours > 0 else defaults.min_window
        return cls(rate_limit_threshold=threshold, min_window=window)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubClient:
    """Synchronous GitHub client. Counts REST calls (`api_calls`) and raw fetches (`raw_calls`)."""

    def __init__(
        self,
        token: str | None = None,
        config: GitHubConfig | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config or GitHubConfig.from_env()
        resolved = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pipelineiq-collector",
        }
        if resolved:
            headers["Authorization"] = f"Bearer {resolved}"
        # DEFAULT: sync httpx.Client; the collector is a sequential CLI. httpx drops the
        # Authorization header on cross-origin redirects (job logs redirect to blob storage).
        self._http = httpx.Client(
            headers=headers,
            timeout=self.config.timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._clock = clock
        self._sleep = sleep
        self._remaining: int | None = None
        self._reset: float | None = None
        self.api_calls = 0
        self.raw_calls = 0

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- low level -------------------------------------------------------------------------

    def _backoff(self, attempt: int, floor: float = 0.0) -> float:
        return max(
            floor, min(self.config.backoff_cap, self.config.backoff_base * float(2**attempt))
        )

    def _wait_for_quota(self) -> None:
        if self._remaining is None or self._reset is None:
            return
        if self._remaining < self.config.rate_limit_threshold:
            delay = self._reset - self._clock() + 1.0
            if delay > 0:
                logger.warning(
                    "rate limit low, sleeping until reset",
                    extra={"remaining": self._remaining, "sleep_s": round(delay, 1)},
                )
                self._sleep(delay)
            # Headers are stale after the reset; wait for fresh ones.
            self._remaining = None
            self._reset = None

    def _record_rate_limit(self, response: httpx.Response) -> None:
        # DEFAULT: read the final answer's headers first. After a redirect to blob storage (job
        # and run logs) the final answer has none, so fall back to the last earlier answer in the
        # chain that has them (the API's own 302), which carries the quota.
        for answer in (response, *reversed(response.history)):
            remaining = answer.headers.get("x-ratelimit-remaining")
            reset = answer.headers.get("x-ratelimit-reset")
            if remaining is None or reset is None:
                continue
            try:
                self._remaining, self._reset = int(remaining), float(reset)
            except ValueError:
                continue
            return

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    def _wait_for(self, response: httpx.Response, attempt: int, rest: bool) -> float | None:
        """Seconds to wait before retrying this response, or None if it is final."""
        status = response.status_code
        if status >= 500:
            return self._backoff(attempt)
        if status not in (403, 429):
            return None
        retry_after = self._retry_after(response)
        if retry_after is not None:
            return retry_after
        if status == 429:
            return self._backoff(attempt, floor=self.config.secondary_backoff if rest else 0.0)
        if rest and self._remaining == 0 and self._reset is not None:
            wait = max(0.0, self._reset - self._clock() + 1.0)
            self._remaining = self._reset = None
            return wait
        if "rate limit" in response.text.lower():
            return self._backoff(attempt, floor=self.config.secondary_backoff)
        return None

    def _get_capped(
        self, url: str, params: dict[str, Any] | None, max_bytes: int
    ) -> httpx.Response:
        """GET that holds at most `max_bytes` of a successful body, plus one decoded chunk."""
        request = self._http.build_request("GET", url, params=params)
        response = self._http.send(request, stream=True)  # follows redirects like `get`
        try:
            if not response.is_success:
                response.read()  # error bodies are small and the callers look at them
                return response
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise GitHubTooLargeError(
                    f"body is {declared} bytes, over {max_bytes}",
                    max_bytes=max_bytes,
                    content_length=int(declared),
                )
            # BytesIO.getvalue() hands out its buffer without a copy, and httpx keeps that same
            # object, so the body is held once (a bytearray plus bytes() would hold it twice).
            body = io.BytesIO()
            for chunk in response.iter_bytes():  # already decoded, so no content-encoding is left
                body.write(chunk)
                if body.tell() > max_bytes:
                    raise GitHubTooLargeError(
                        f"body is over {max_bytes} bytes", max_bytes=max_bytes
                    )
            return httpx.Response(
                response.status_code,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
                },
                content=body.getvalue(),
                request=request,
                history=response.history,
            )
        finally:
            response.close()

    def _send(
        self,
        url: str,
        params: dict[str, Any] | None,
        *,
        rest: bool,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        """GET with rate-limit awareness and retries. Returns any non-retryable response."""
        kind = "rest" if rest else "raw"
        for attempt in range(self.config.max_retries + 1):
            if rest:
                self._wait_for_quota()
                self.api_calls += 1
            else:
                self.raw_calls += 1
            try:
                if max_bytes is None:
                    response = self._http.get(url, params=params)
                else:
                    response = self._get_capped(url, params, max_bytes)
            except httpx.TransportError as exc:
                if attempt == self.config.max_retries:
                    raise GitHubError(f"transport error: {type(exc).__name__}") from exc
                delay = self._backoff(attempt)
                logger.warning(
                    "transport error, retrying",
                    extra={"kind": kind, "error": type(exc).__name__, "sleep_s": delay},
                )
                self._sleep(delay)
                continue

            if rest:
                self._record_rate_limit(response)
            wait = self._wait_for(response, attempt, rest)
            if wait is None:
                return response
            if attempt == self.config.max_retries:
                raise GitHubError(
                    f"gave up after retries (HTTP {response.status_code})", response.status_code
                )
            logger.warning(
                "retrying request",
                extra={
                    "kind": kind,
                    "status": response.status_code,
                    "attempt": attempt + 1,
                    "sleep_s": wait,
                },
            )
            self._sleep(wait)
        raise GitHubError("unreachable")  # pragma: no cover

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.config.base_url}{path}"

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code == 404:
            raise GitHubNotFoundError("not found", 404)
        if response.is_error:
            message = ""
            try:
                message = str(response.json().get("message", ""))[:200]
            except (ValueError, AttributeError):
                pass
            raise GitHubError(f"HTTP {response.status_code}: {message}", response.status_code)

    # ---- REST ------------------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET a REST path (or absolute API URL); raises GitHubError on failure."""
        response = self._send(self._url(path), params, rest=True)
        self._check(response)
        return response

    def write_json(self, method: str, path: str, body: dict[str, Any]) -> Any:
        """POST or PATCH a JSON body once and return the decoded reply (raises GitHubError).

        # DEFAULT: writes are not retried (a retried POST could create a duplicate check run);
        # the caller decides what a failure means. Rate-limit headers are still recorded.
        """
        self._wait_for_quota()
        self.api_calls += 1
        try:
            response = self._http.request(method, self._url(path), json=body)
        except httpx.TransportError as exc:
            raise GitHubError(f"transport error: {type(exc).__name__}") from exc
        self._record_rate_limit(response)
        self._check(response)
        return response.json()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a REST path and return the decoded JSON body."""
        return self.get(path, params).json()

    def get_bytes(
        self, path: str, params: dict[str, Any] | None = None, *, max_bytes: int | None = None
    ) -> bytes | None:
        """GET a body such as a job log (follows redirects); None on 404.

        With `max_bytes`, a larger body raises `GitHubTooLargeError` without being read fully.
        """
        response = self._send(self._url(path), params, rest=True, max_bytes=max_bytes)
        if response.status_code == 404:
            return None
        self._check(response)
        return response.content

    def get_rate_limit(self) -> dict[str, Any]:
        """Return the `GET /rate_limit` body (this call does not use quota)."""
        result: dict[str, Any] = self.get_json("/rate_limit")
        return result

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        items_key: str | None = None,
    ) -> Iterator[Any]:
        """Yield items across all pages, following Link rel=next; `items_key` names a list field."""
        merged = {"per_page": 100, **(params or {})}
        response: httpx.Response | None = self.get(path, merged)
        while response is not None:
            body = response.json()
            yield from (body[items_key] if items_key else body)
            next_url = response.links.get("next", {}).get("url")
            response = self.get(next_url) if next_url else None

    def list_shrinking(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        items_key: str | None = None,
    ) -> list[Any]:
        """Return all items like `paginate`, retrying with smaller pages after a 5xx.

        A listing that GitHub cannot serve at 100 per page (HTTP 5xx after the normal retries, on
        any page) is restarted from page 1 with each of `fallback_page_sizes`. The result is a
        complete list from one attempt, so nothing is missing or duplicated. Other errors, and a
        5xx at the smallest size, are raised.
        """
        base = dict(params or {})
        sizes = [int(base.pop("per_page", 100)), *self.config.fallback_page_sizes]
        for index, size in enumerate(sizes):
            try:
                return list(self.paginate(path, {**base, "per_page": size}, items_key=items_key))
            except GitHubError as exc:
                if not exc.is_server_error or index == len(sizes) - 1:
                    raise
                logger.warning(
                    "listing failed with a server error; retrying with smaller pages",
                    extra={
                        "path": path,
                        "status": exc.status_code,
                        "next_per_page": sizes[index + 1],
                    },
                )
        raise GitHubError("unreachable")  # pragma: no cover

    def iter_workflow_runs(
        self,
        full_name: str,
        start: datetime,
        end: datetime,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield workflow runs created in [start, end].

        Windows that hit the 1,000 cap are split. Windows that come back empty are not believed
        straight away: GitHub silently returns `total_count: 0` for some slow filtered queries
        (see `_confirm_empty`).
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        yield from self._runs_window(full_name, start, end, params or {}, verify=True)

    def count_workflow_runs(
        self,
        full_name: str,
        start: datetime,
        end: datetime,
        params: dict[str, Any] | None = None,
    ) -> RunCount:
        """Count runs created in [start, end] with one cheap call (`per_page=1`).

        A non-zero `total_count` is trusted; a zero is cross-checked like in `iter_workflow_runs`.
        A count of 2,500 or more is returned as capped ("at least"), never as exact.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        return self._count_window(full_name, start, end, params or {}, verify=True)

    @staticmethod
    def _window_query(
        start: datetime, end: datetime, params: dict[str, Any], per_page: int
    ) -> dict[str, Any]:
        return {"per_page": per_page, **params, "created": f"{_iso(start)}..{_iso(end)}"}

    @staticmethod
    def _split(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        # Halve; the right half starts one second later so the halves never overlap.
        mid = (start + (end - start) / 2).replace(microsecond=0)
        return mid, mid + timedelta(seconds=1)

    def _first_page(
        self,
        full_name: str,
        start: datetime,
        end: datetime,
        params: dict[str, Any],
        per_page: int,
        verify: bool,
    ) -> httpx.Response:
        """Fetch a window's first page; an empty answer is re-asked `empty_retries` times."""
        path = f"/repos/{full_name}/actions/runs"
        query = self._window_query(start, end, params, per_page)
        response = self.get(path, query)
        retries = self.config.empty_retries if verify else 0
        for attempt in range(retries):
            body = response.json()
            if body.get("workflow_runs") or int(body.get("total_count", 0)) > 0:
                break
            logger.info(
                "empty run window, asking again",
                extra={"repo": full_name, "start": _iso(start), "end": _iso(end)},
            )
            self._sleep(self._backoff(attempt))
            response = self.get(path, query)
        return response

    def _runs_window(
        self,
        full_name: str,
        start: datetime,
        end: datetime,
        params: dict[str, Any],
        *,
        verify: bool,
    ) -> Iterator[dict[str, Any]]:
        response = self._first_page(full_name, start, end, params, 100, verify)
        body = response.json()
        total = int(body.get("total_count", 0))
        if total >= RESULT_CAP:
            if end - start > self.config.min_window:
                mid, right = self._split(start, end)
                logger.info(
                    "window hit result cap, splitting",
                    extra={"repo": full_name, "start": _iso(start), "end": _iso(end)},
                )
                yield from self._runs_window(full_name, start, mid, params, verify=verify)
                yield from self._runs_window(full_name, right, end, params, verify=verify)
                return
            logger.warning(
                "window at minimum size still hits result cap; results truncated",
                extra={"repo": full_name, "start": _iso(start), "end": _iso(end), "total": total},
            )
        elif not body.get("workflow_runs") and verify:
            yield from self._confirm_empty(full_name, start, end, params)
            return
        while True:
            yield from body.get("workflow_runs", [])
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                return
            response = self.get(next_url)
            body = response.json()

    def _confirm_empty(
        self, full_name: str, start: datetime, end: datetime, params: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """Cross-check a window that stayed empty after retries by asking for its two halves.

        # DEFAULT: one level only (two extra calls, halves asked once each, no further checks),
        # so a genuinely quiet window costs at most 1 + empty_retries + 2 calls and never loops.
        Both halves empty: accept "no runs". Otherwise GitHub contradicted itself: warn and use
        the halves' runs.
        """
        if end - start <= self.config.min_window:
            return
        mid, right = self._split(start, end)
        found = False
        for lo, hi in ((start, mid), (right, end)):
            for run in self._runs_window(full_name, lo, hi, params, verify=False):
                found = True
                yield run
        if found:
            logger.warning(
                "GitHub returned no runs for a window but its halves have runs; using the halves",
                extra={"repo": full_name, "start": _iso(start), "end": _iso(end)},
            )

    def _count_window(
        self,
        full_name: str,
        start: datetime,
        end: datetime,
        params: dict[str, Any],
        *,
        verify: bool,
    ) -> RunCount:
        response = self._first_page(full_name, start, end, params, 1, verify)
        total = int(response.json().get("total_count", 0))
        if total >= TOTAL_COUNT_CAP:
            return RunCount(total, capped=True)
        if total > 0 or not verify or end - start <= self.config.min_window:
            return RunCount(total)
        mid, right = self._split(start, end)
        left = self._count_window(full_name, start, mid, params, verify=False)
        other = self._count_window(full_name, right, end, params, verify=False)
        count = left.count + other.count
        if count:
            logger.warning(
                "GitHub counted no runs for a window but its halves have runs; using the halves",
                extra={"repo": full_name, "start": _iso(start), "end": _iso(end)},
            )
        return RunCount(count, capped=left.capped or other.capped)

    # ---- raw.githubusercontent.com ---------------------------------------------------------

    def fetch_raw_file(self, full_name: str, sha: str, path: str) -> bytes | None:
        """Fetch a file at a commit from raw.githubusercontent.com; None if it does not exist."""
        url = f"{self.config.raw_url}/{full_name}/{sha}/{path.lstrip('/')}"
        response = self._send(url, None, rest=False)
        if response.status_code == 404:
            return None
        self._check(response)
        return response.content
