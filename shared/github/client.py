"""GitHub REST client: token auth, pagination, rate limits, retries and date-window splitting."""

from __future__ import annotations

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


class GitHubError(Exception):
    """A GitHub request failed for good (after retries, or a non-retryable status)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubNotFoundError(GitHubError):
    """The resource does not exist (404), or is not visible to the token."""


class GitHubResponseTooLargeError(GitHubError):
    """A streamed response exceeded the caller's maximum byte limit."""

    def __init__(self, max_bytes: int, content_length: int | None = None) -> None:
        super().__init__(f"response exceeds {max_bytes} bytes", 413)
        self.max_bytes = max_bytes
        self.content_length = content_length


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
    # DEFAULT: 30 s request timeout.
    timeout: float = 30.0
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
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None and reset is not None:
            try:
                self._remaining, self._reset = int(remaining), float(reset)
            except ValueError:
                pass

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

    def _send(self, url: str, params: dict[str, Any] | None, *, rest: bool) -> httpx.Response:
        """GET with rate-limit awareness and retries. Returns any non-retryable response."""
        kind = "rest" if rest else "raw"
        for attempt in range(self.config.max_retries + 1):
            if rest:
                self._wait_for_quota()
                self.api_calls += 1
            else:
                self.raw_calls += 1
            try:
                response = self._http.get(url, params=params)
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

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a REST path and return the decoded JSON body."""
        return self.get(path, params).json()

    def get_bytes(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        """GET a body such as a job log (follows redirects); None on 404."""
        if max_bytes is not None:
            if max_bytes < 1:
                raise ValueError("max_bytes must be positive")
            return self._get_bytes_limited(self._url(path), params, max_bytes)
        response = self._send(self._url(path), params, rest=True)
        if response.status_code == 404:
            return None
        self._check(response)
        return response.content

    def _get_bytes_limited(
        self,
        url: str,
        params: dict[str, Any] | None,
        max_bytes: int,
    ) -> bytes | None:
        """Stream a response and stop as soon as it exceeds a byte limit."""
        for attempt in range(self.config.max_retries + 1):
            self._wait_for_quota()
            self.api_calls += 1
            retry_wait: float | None = None
            retry_status_code: int | None = None
            try:
                with self._http.stream("GET", url, params=params) as response:
                    if response.status_code == 404:
                        return None
                    self._record_rate_limit(response)
                    if response.is_error:
                        response.read()
                        retry_wait = self._wait_for(response, attempt, rest=True)
                        if retry_wait is None:
                            self._check(response)
                        retry_status_code = response.status_code
                    else:
                        content_length_value = response.headers.get("content-length")
                        try:
                            content_length = (
                                int(content_length_value)
                                if content_length_value is not None
                                else None
                            )
                        except ValueError:
                            content_length = None
                        if content_length is not None and content_length > max_bytes:
                            raise GitHubResponseTooLargeError(max_bytes, content_length)
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if len(body) + len(chunk) > max_bytes:
                                raise GitHubResponseTooLargeError(max_bytes)
                            body.extend(chunk)
                        return bytes(body)
            except httpx.TransportError as exc:
                if attempt == self.config.max_retries:
                    raise GitHubError(f"transport error: {type(exc).__name__}") from exc
                retry_wait = self._backoff(attempt)
                logger.warning(
                    "transport error, retrying",
                    extra={
                        "kind": "rest",
                        "error": type(exc).__name__,
                        "sleep_s": retry_wait,
                    },
                )
            if retry_wait is None:
                raise GitHubError("unreachable")  # pragma: no cover
            if attempt == self.config.max_retries:
                raise GitHubError("gave up after retries", retry_status_code)
            self._sleep(retry_wait)
        raise GitHubError("unreachable")  # pragma: no cover

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

    def iter_workflow_runs(
        self,
        full_name: str,
        start: datetime,
        end: datetime,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield workflow runs created in [start, end], splitting windows that hit the 1,000 cap."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        yield from self._runs_window(full_name, start, end, params or {})

    def _runs_window(
        self, full_name: str, start: datetime, end: datetime, params: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        query = {"per_page": 100, **params, "created": f"{_iso(start)}..{_iso(end)}"}
        response = self.get(f"/repos/{full_name}/actions/runs", query)
        body = response.json()
        total = int(body.get("total_count", 0))
        if total >= RESULT_CAP:
            if end - start > self.config.min_window:
                # Halve; the right half starts one second later so the halves never overlap.
                mid = (start + (end - start) / 2).replace(microsecond=0)
                logger.info(
                    "window hit result cap, splitting",
                    extra={"repo": full_name, "start": _iso(start), "end": _iso(end)},
                )
                yield from self._runs_window(full_name, start, mid, params)
                yield from self._runs_window(full_name, mid + timedelta(seconds=1), end, params)
                return
            logger.warning(
                "window at minimum size still hits result cap; results truncated",
                extra={"repo": full_name, "start": _iso(start), "end": _iso(end), "total": total},
            )
        while True:
            yield from body.get("workflow_runs", [])
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                return
            response = self.get(next_url)
            body = response.json()

    # ---- raw.githubusercontent.com ---------------------------------------------------------

    def fetch_raw_file(self, full_name: str, sha: str, path: str) -> bytes | None:
        """Fetch a file at a commit from raw.githubusercontent.com; None if it does not exist."""
        url = f"{self.config.raw_url}/{full_name}/{sha}/{path.lstrip('/')}"
        response = self._send(url, None, rest=False)
        if response.status_code == 404:
            return None
        self._check(response)
        return response.content
