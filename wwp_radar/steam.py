"""Steam endpoints on top of the shared HttpClient (timeouts, retries, backoff, rate limits).

Endpoints (verified 2026-09):
  * ISteamUserStats/GetNumberOfCurrentPlayers/v1 -> {"response": {"player_count": N, "result": 1}}
    For apps without data (e.g. not yet released) Steam answers HTTP 404 with the JSON body
    {"response": {"result": 42}} and no player_count (observed 2026-09-25 for app 3222640).
    That is "unavailable", *not* a zero-player observation.
  * store.steampowered.com/appreviews/<appid>?json=1 -> {"success": 1, "query_summary": {...},
    "reviews": [...], "cursor": "..."}; cursor pagination, max 100 per page.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .httpclient import HttpClient, ShutdownRequested, SourceError

# Steam-flavoured names used throughout the collector and tests.
SteamError = SourceError
__all__ = ["SteamClient", "SteamError", "ShutdownRequested", "CcuResult"]

CCU_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
REVIEWS_URL = "https://store.steampowered.com/appreviews/{app_id}"


@dataclass
class CcuResult:
    status: str  # ok | unavailable
    player_count: int | None
    http_status: int
    attempts: int
    detail: str | None = None


class SteamClient(HttpClient):
    source_name = "Steam"

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

