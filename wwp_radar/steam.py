"""Steam HTTP client with timeouts, bounded retries, backoff and rate-limit handling.

Endpoints (verified 2026-09):
  * ISteamUserStats/GetNumberOfCurrentPlayers/v1 -> {"response": {"player_count": N, "result": 1}}
    For apps without data (e.g. not yet released) Steam answers HTTP 404 with the JSON body
    {"response": {"result": 42}} and no player_count (observed 2026-09-25 for app 3222640).
    That is "unavailable", *not* a zero-player observation.
  * store.steampowered.com/appreviews/<appid>?json=1 -> {"success": 1, "query_summary": {...},
    "reviews": [...], "cursor": "..."}; cursor pagination, max 100 per page.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from . import __version__

log = logging.getLogger(__name__)

CCU_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
REVIEWS_URL = "https://store.steampowered.com/appreviews/{app_id}"

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SteamError(Exception):
    def __init__(self, message: str, http_status: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.http_status = http_status
        self.attempts = attempts


class ShutdownRequested(Exception):
    pass


@dataclass
class CcuResult:
    status: str  # ok | unavailable
    player_count: int | None
    http_status: int
    attempts: int
    detail: str | None = None


class SteamClient:
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

    # -- low level -------------------------------------------------------------
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

    def get_json(self, url: str, params: dict[str, Any], json_statuses: frozenset[int] = frozenset()) -> tuple[Any, int, int]:
        """GET with retries. Returns (json, http_status, attempts) or raises SteamError.

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
                    resp = self._client.get(url, params=params)
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
                            raise SteamError(f"HTTP {resp.status_code}", resp.status_code, attempts)
                    elif resp.status_code in RETRYABLE_STATUS:
                        last_error = f"HTTP {resp.status_code}"
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    else:
                        raise SteamError(f"HTTP {resp.status_code}", resp.status_code, attempts)
            if attempt >= self.max_retries:
                break
            delay = min(60.0, self.backoff_base * (2 ** attempt)) * (0.75 + random.random() * 0.5)
            if retry_after is not None:
                delay = max(delay, min(retry_after, 300.0))
            log.warning("Steam request failed (%s), retry %d/%d in %.1fs", last_error, attempt + 1, self.max_retries, delay)
            self._sleep(delay)
        raise SteamError(last_error, last_status, attempts)

    # -- endpoints ---------------------------------------------------------------
    def current_players(self, app_id: int) -> CcuResult:
        data, status, attempts = self.get_json(CCU_URL, {"appid": app_id}, json_statuses=frozenset({404}))
        resp = data.get("response") if isinstance(data, dict) else None
        if not isinstance(resp, dict):
            raise SteamError("unexpected response shape", status, attempts)
        result = resp.get("result")
        count = resp.get("player_count")
        if status == 200 and result == 1 and isinstance(count, int) and count >= 0:
            return CcuResult("ok", count, status, attempts)
        if result == 42 or (result is not None and count is None):
            # Steam has no data for this app (unreleased, unknown, or temporarily unavailable).
            return CcuResult("unavailable", None, status, attempts, detail=f"Steam result={result}, no player_count")
        raise SteamError(f"unexpected CCU payload: {resp!r}"[:300], status, attempts)

    def reviews_page(self, app_id: int, params: dict[str, Any], cursor: str) -> tuple[dict, int]:
        query = {"json": 1, **params, "cursor": cursor}
        data, status, attempts = self.get_json(REVIEWS_URL.format(app_id=app_id), query)
        if not isinstance(data, dict) or data.get("success") != 1:
            raise SteamError(f"reviews endpoint returned success={data.get('success') if isinstance(data, dict) else '?'}", status, attempts)
        if not isinstance(data.get("reviews", []), list):
            raise SteamError("reviews field is not a list", status, attempts)
        return data, attempts


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
