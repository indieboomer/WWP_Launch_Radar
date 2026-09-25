"""Deterministic fixtures: a fake Steam backend served through httpx.MockTransport."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from wwp_radar import store
from wwp_radar.config import Settings
from wwp_radar.db import Database, transaction
from wwp_radar.steam import SteamClient

APP_ID = 3222640


def make_review(i: int, created: int, *, up: bool = True, text: str | None = None, updated: int | None = None,
                steam: bool = True, lang: str = "english", votes: int = 0) -> dict:
    return {
        "recommendationid": str(1000 + i),
        "author": {"steamid": str(76561198000000000 + i), "playtime_at_review": 60 + i, "playtime_forever": 120 + i},
        "language": lang,
        "review": text if text is not None else f"review {i}",
        "timestamp_created": created,
        "timestamp_updated": updated or created,
        "voted_up": up,
        "votes_up": votes,
        "votes_funny": 0,
        "weighted_vote_score": "0.5",
        "comment_count": 0,
        "steam_purchase": steam,
        "received_for_free": not steam,
        "refunded": False,
        "written_during_early_access": True,
        "primarily_steam_deck": False,
    }


class FakeSteam:
    """In-memory Steam: GetNumberOfCurrentPlayers + appreviews with offset-based cursors."""

    def __init__(self):
        self.reviews: dict[str, dict] = {}
        self.ccu_responses: list = []  # items: int | "unavailable" | int http status error | Exception
        self.requests: list[dict] = []
        self.fail_review_requests = 0

    def add(self, r: dict) -> None:
        self.reviews[r["recommendationid"]] = r

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append({"path": url.path, **q})
        if "GetNumberOfCurrentPlayers" in url.path:
            item = self.ccu_responses.pop(0) if self.ccu_responses else 0
            if isinstance(item, Exception):
                raise item
            if item == "unavailable":  # what Steam really returns for an app without CCU data
                return httpx.Response(404, json={"response": {"result": 42}})
            if isinstance(item, tuple):  # (status,)
                return httpx.Response(item[0], text="error")
            return httpx.Response(200, json={"response": {"player_count": item, "result": 1}})
        if self.fail_review_requests > 0:
            self.fail_review_requests -= 1
            return httpx.Response(503, text="busy")
        key = "timestamp_updated" if q.get("filter") == "updated" else "timestamp_created"
        items = [r for r in self.reviews.values()
                 if q.get("purchase_type", "all") == "all" or r["steam_purchase"]]
        items.sort(key=lambda r: (r[key], r["recommendationid"]), reverse=True)
        cursor = q.get("cursor", "*")
        offset = 0 if cursor == "*" else int(cursor.split(":")[1])
        size = int(q.get("num_per_page", 20))
        page = items[offset:offset + size]
        body = {"success": 1, "reviews": page, "cursor": f"c:{offset + len(page)}" if page else cursor}
        if cursor == "*":
            pos = sum(1 for r in items if r["voted_up"])
            body["query_summary"] = {"num_reviews": len(page), "review_score": 6, "review_score_desc": "Mostly Positive",
                                     "total_positive": pos, "total_negative": len(items) - pos, "total_reviews": len(items)}
        return httpx.Response(200, content=json.dumps(body))


@pytest.fixture
def fake_steam():
    return FakeSteam()


@pytest.fixture
def client(fake_steam):
    c = SteamClient(transport=httpx.MockTransport(fake_steam.handler), max_retries=2, min_request_gap=0,
                    sleep=lambda s: None, backoff_base=0.01)
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, ccu_interval=60, review_interval=120)


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def game_id(db):
    conn = db.conn()
    with transaction(conn):
        gid = store.ensure_game(conn, APP_ID, "Wild West Pioneers")
        store.set_setting(conn, "active_game_id", str(gid))
    return gid
