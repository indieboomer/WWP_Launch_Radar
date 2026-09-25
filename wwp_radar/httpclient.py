"""Shared HTTP client: timeouts, bounded retries, backoff with jitter, rate-limit handling,
bounded concurrency and a minimum gap between requests. Used by the Steam and Twitch clients;
each source gets its own instance, so their limits are independent.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable

import httpx

from . import __version__

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SourceError(Exception):
    def __init__(self, message: str, http_status: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.http_status = http_status
        self.attempts = attempts


class ShutdownRequested(Exception):
    pass


class HttpClient:
    source_name = "HTTP"

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_retries: int = 4,
        max_concurrency: int = 2,
        min_request_gap: float = 1.0,
        stop_event: threading.Event | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff_base: float = 2.0,
    ):
        self.max_retries = max_retries
        self.min_request_gap = min_request_gap
        self.backoff_base = backoff_base
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._gap_lock = threading.Lock()
        self._last_request = 0.0
        self._stop = stop_event or threading.Event()
        self._sleep_fn = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            headers={"User-Agent": f"WWP-Launch-Radar/{__version__} (+local monitoring)"},
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleep_fn is not None:
            self._sleep_fn(seconds)
            return
        if self._stop.wait(seconds):
            raise ShutdownRequested()

    def _respect_gap(self) -> None:
        with self._gap_lock:
            wait = self._last_request + self.min_request_gap - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()

    def request_json(self, method: str, url: str, params: Any = None, *, headers: dict[str, str] | None = None,
                     json_statuses: frozenset[int] = frozenset()) -> tuple[Any, int, int]:
        """Request with retries. Returns (json, http_status, attempts) or raises SourceError.

        json_statuses: non-200 statuses whose JSON body is still meaningful to the caller.
        """
        last_error = "unknown error"
        last_status: int | None = None
        attempts = 0
        for attempt in range(self.max_retries + 1):
            if self._stop.is_set():
                raise ShutdownRequested()
            attempts = attempt + 1
            retry_after: float | None = None
            with self._sem:
                self._respect_gap()
                try:
                    resp = self._client.request(method, url, params=params, headers=headers)
                except httpx.TimeoutException as e:
                    last_error, last_status = f"timeout: {e.__class__.__name__}", None
                except httpx.HTTPError as e:
                    last_error, last_status = f"network error: {e.__class__.__name__}: {e}", None
                else:
                    last_status = resp.status_code
                    if resp.status_code == 200:
                        try:
                            return resp.json(), 200, attempts
                        except ValueError:
                            last_error = "invalid JSON in response"
                            # Treat as retryable: Steam occasionally serves HTML error pages with 200.
                    elif resp.status_code in json_statuses:
                        try:
                            return resp.json(), resp.status_code, attempts
                        except ValueError:
                            raise SourceError(f"HTTP {resp.status_code}", resp.status_code, attempts)
                    elif resp.status_code in RETRYABLE_STATUS:
                        last_error = f"HTTP {resp.status_code}"
                        retry_after = _retry_after(resp.headers)
                    else:
                        raise SourceError(f"HTTP {resp.status_code}", resp.status_code, attempts)
            if attempt >= self.max_retries:
                break
            delay = min(60.0, self.backoff_base * (2 ** attempt)) * (0.75 + random.random() * 0.5)
            if retry_after is not None:
                delay = max(delay, min(retry_after, 300.0))
            log.warning("%s request failed (%s), retry %d/%d in %.1fs", self.source_name, last_error, attempt + 1,
                        self.max_retries, delay)
            self._sleep(delay)
        raise SourceError(last_error, last_status, attempts)

    def get_json(self, url: str, params: Any = None, json_statuses: frozenset[int] = frozenset(),
                 headers: dict[str, str] | None = None) -> tuple[Any, int, int]:
        return self.request_json("GET", url, params, headers=headers, json_statuses=json_statuses)


def _retry_after(headers: httpx.Headers) -> float | None:
    """Retry-After (seconds), or Twitch's Ratelimit-Reset (epoch seconds when the bucket refills)."""
    value = headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    reset = headers.get("Ratelimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return None
