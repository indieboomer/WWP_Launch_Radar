"""Twitch live-stream monitoring for the game's Twitch category (Helix API).

Endpoints:
  * POST id.twitch.tv/oauth2/token (client credentials) -> {"access_token", "expires_in", ...}
    An app access token is enough: every endpoint used here reads public data.
  * GET  api.twitch.tv/helix/games?name=<name> | ?id=<id> -> {"data": [{"id", "name", ...}]}
  * GET  api.twitch.tv/helix/streams?game_id=<id>&type=live&first=100&after=<cursor>
    -> {"data": [{"id", "user_id", "user_login", "user_name", "title", "viewer_count",
                  "started_at", "language", "tags", "is_mature", ...}], "pagination": {"cursor"}}

Semantics (same rules as CCU):
  * One poll = one snapshot of every live stream in the category, walked across all pages.
    A snapshot is stored only when the whole walk succeeded. A failed or partial walk is logged
    in collection_runs and never stored, so a failure never looks like "0 viewers".
  * A successful walk with no live streams IS stored, as a real 0.
  * An unknown category (not on Twitch yet) is "unavailable", not zero.
  * Streams can move between pages while we paginate; they are deduplicated by stream id.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from . import store
from .db import Database, now, transaction
from .httpclient import HttpClient, SourceError

log = logging.getLogger(__name__)

TwitchError = SourceError

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
PAGE_SIZE = 100
MAX_PAGES = 50  # 5000 live streams per snapshot; far above what one indie category reaches


@dataclass
class Category:
    id: str
    name: str


class TwitchClient(HttpClient):
    source_name = "Twitch"

    def __init__(self, client_id: str, client_secret: str, **kw):
        super().__init__(**kw)
        self.client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    def _access_token(self) -> str:
        with self._token_lock:
            if self._token and time.time() < self._token_expires - 300:
                return self._token
            try:
                data, _, _ = self.request_json("POST", TOKEN_URL, {
                    "client_id": self.client_id, "client_secret": self._client_secret,
                    "grant_type": "client_credentials"})
            except SourceError as e:
                if e.http_status in (400, 401, 403):
                    raise SourceError(f"Twitch rejected the client credentials (HTTP {e.http_status})",
                                      e.http_status, e.attempts) from None
                raise
            token = data.get("access_token") if isinstance(data, dict) else None
            if not token:
                raise SourceError("token response without access_token", 200)
            self._token = token
            self._token_expires = time.time() + float(data.get("expires_in") or 3600)
            return token

    def helix(self, path: str, params) -> tuple[dict, int]:
        """GET a Helix endpoint. A 401 (token expired or revoked) refreshes the token once."""
        attempts = 0
        for retry in range(2):
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self._access_token()}"}
            try:
                data, _, a = self.get_json(f"{HELIX}/{path}", params, headers=headers)
            except SourceError as e:
                if e.http_status == 401 and retry == 0:
                    with self._token_lock:
                        self._token = None
                    attempts += e.attempts
                    continue
                e.attempts += attempts
                raise
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise SourceError(f"unexpected Helix payload for {path}", 200, attempts + a)
            return data, attempts + a
        raise AssertionError("unreachable")

    def find_category(self, query: str) -> Category | None:
        """Look a category up by exact name, or by id when the query is numeric."""
        query = query.strip()
        lookups = [{"id": query}, {"name": query}] if query.isdigit() else [{"name": query}]
        for params in lookups:
            data, _ = self.helix("games", params)
            if data["data"]:
                g = data["data"][0]
                return Category(str(g["id"]), str(g.get("name") or query))
        return None

    def live_streams(self, category_id: str) -> tuple[list[dict], int, int]:
        """All live streams in the category. Returns (streams, pages, attempts)."""
        seen: dict[str, dict] = {}
        cursor = None
        pages = attempts = 0
        while pages < MAX_PAGES:
            params = {"game_id": category_id, "type": "live", "first": PAGE_SIZE}
            if cursor:
                params["after"] = cursor
            data, a = self.helix("streams", params)
            pages += 1
            attempts += a
            for s in data["data"]:
                seen.setdefault(str(s["id"]), s)
            cursor = (data.get("pagination") or {}).get("cursor")
            if not data["data"] or not cursor:
                return list(seen.values()), pages, attempts
        raise SourceError(f"more than {MAX_PAGES} pages of live streams; snapshot not stored", 200, attempts)


def category_query(game: dict, default: str) -> str:
    return (game.get("twitch_category") or default or game["name"]).strip()


def poll_once(db: Database, client: TwitchClient, game_id: int, default_category: str = "") -> str:
    """Take one snapshot of the game's Twitch category. Returns the run status."""
    conn = db.conn()
    started = now()
    game = dict(store.get_game(conn, game_id))
    with transaction(conn):
        store.mark_twitch_started(conn, game_id, started)
    try:
        cat_id = game.get("twitch_category_id")
        if not cat_id:
            query = category_query(game, default_category)
            cat = client.find_category(query)
            if cat is None:
                with transaction(conn):
                    store.record_run(conn, game_id, "twitch", started, "unavailable", http_status=200,
                                     error=f"category {query!r} not found on Twitch")
                return "unavailable"
            with transaction(conn):
                store.set_twitch_category(conn, game_id, query, cat.id, cat.name)
            cat_id = cat.id
        streams, pages, attempts = client.live_streams(cat_id)
    except SourceError as e:
        with transaction(conn):
            store.record_run(conn, game_id, "twitch", started, "error", attempts=e.attempts,
                             http_status=e.http_status, error=str(e))
        return "error"
    with transaction(conn):
        store.insert_twitch_snapshot(conn, game_id, now(), cat_id, streams, pages)
        store.record_run(conn, game_id, "twitch", started, "ok", attempts=attempts, http_status=200, items=len(streams))
    return "ok"


def twitch_loop(collector) -> None:
    settings = collector.settings
    interval = settings.twitch_interval
    next_at = time.time()
    while not collector.stop_event.is_set():
        collector.heartbeat["twitch"] = now()
        game = collector._game()
        if game:
            poll_once(collector.db, collector.twitch_client, game[0], settings.twitch_category)
        next_at += interval
        delay = next_at - time.time()
        if delay < 0:  # fell behind (sleep/suspend): realign without a burst of catch-up requests
            next_at = time.time() + interval
            delay = interval
        if collector.stop_event.wait(delay):
            return
