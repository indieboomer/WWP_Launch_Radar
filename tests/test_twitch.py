"""Twitch monitoring with a fake Helix backend (httpx.MockTransport), no network."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from wwp_radar import aggregates, exports, store, twitch
from wwp_radar.app import create_app
from wwp_radar.db import transaction

WAW = ZoneInfo("Europe/Warsaw")
CAT = "5550001"


def stream(i: int, viewers: int, *, user: int | None = None, title: str | None = None, lang: str = "en") -> dict:
    u = user if user is not None else i
    return {"id": f"s{i}", "user_id": f"u{u}", "user_login": f"streamer{u}", "user_name": f"Streamer{u}",
            "game_id": CAT, "game_name": "Wild West Pioneers", "type": "live", "title": title or f"stream {i}",
            "viewer_count": viewers, "started_at": "2026-10-15T16:00:00Z", "language": lang,
            "tags": ["English"], "is_mature": False}


class FakeTwitch:
    def __init__(self):
        self.categories = {"Wild West Pioneers": CAT}
        self.streams: list[dict] = []
        self.page_size = 2
        self.tokens_issued = 0
        self.valid_tokens: set[str] = set()
        self.fail_streams_after_page: int | None = None
        self.reject_credentials = False
        self.requests: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append((url.path, q))
        if url.path == "/oauth2/token":
            if self.reject_credentials:
                return httpx.Response(403, json={"status": 403, "message": "invalid client secret"})
            self.tokens_issued += 1
            tok = f"tok{self.tokens_issued}"
            self.valid_tokens.add(tok)
            return httpx.Response(200, json={"access_token": tok, "expires_in": 5000000, "token_type": "bearer"})
        auth = request.headers.get("Authorization", "")
        if request.headers.get("Client-Id") != "cid" or auth.removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(401, json={"error": "Unauthorized"})
        if url.path == "/helix/games":
            if "id" in q:
                hit = [(n, i) for n, i in self.categories.items() if i == q["id"]]
            else:
                hit = [(q["name"], self.categories[q["name"]])] if q.get("name") in self.categories else []
            return httpx.Response(200, json={"data": [{"id": i, "name": n, "box_art_url": "", "igdb_id": ""} for n, i in hit]})
        if url.path == "/helix/streams":
            items = sorted([s for s in self.streams if s["game_id"] == q["game_id"]], key=lambda s: -s["viewer_count"])
            offset = int(q.get("after", "0"))
            page_no = offset // self.page_size
            if self.fail_streams_after_page is not None and page_no >= self.fail_streams_after_page:
                return httpx.Response(500, text="boom")
            page = items[offset:offset + self.page_size]
            body = {"data": page, "pagination": {}}
            if offset + self.page_size < len(items):
                body["pagination"]["cursor"] = str(offset + self.page_size)
            return httpx.Response(200, json=body)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Every call to now() in the Twitch module advances 30 s, so each poll gets its own timestamp."""
    ticks = iter(range(1_790_300_000, 1_800_000_000, 30))
    monkeypatch.setattr("wwp_radar.twitch.now", lambda: next(ticks))


@pytest.fixture
def fake_twitch():
    return FakeTwitch()


@pytest.fixture
def tclient(fake_twitch):
    c = twitch.TwitchClient("cid", "secret", transport=httpx.MockTransport(fake_twitch.handler), max_retries=1,
                            min_request_gap=0, sleep=lambda s: None, backoff_base=0.01)
    yield c
    c.close()


def snapshots(db, gid):
    return [dict(r) for r in db.conn().execute("SELECT * FROM twitch_snapshots WHERE game_id=? ORDER BY id", (gid,))]


def last_run(db, gid):
    return dict(db.conn().execute("SELECT * FROM collection_runs WHERE source='twitch' ORDER BY id DESC LIMIT 1").fetchone())


# -- collection -------------------------------------------------------------------------------------

def test_snapshot_walks_all_pages_and_resolves_category(db, game_id, tclient, fake_twitch):
    fake_twitch.streams = [stream(1, 500), stream(2, 120), stream(3, 40), stream(4, 0), stream(5, 7)]
    assert twitch.poll_once(db, tclient, game_id) == "ok"
    snap = snapshots(db, game_id)[0]
    assert (snap["live_channels"], snap["total_viewers"], snap["pages"], snap["category_id"]) == (5, 667, 3, CAT)
    g = store.get_game(db.conn(), game_id)
    assert (g["twitch_category_id"], g["twitch_category_name"]) == (CAT, "Wild West Pioneers")
    assert g["twitch_monitoring_started_at"] is not None
    assert db.conn().execute("SELECT COUNT(*) FROM twitch_stream_observations").fetchone()[0] == 5
    # The category is cached: the second poll does not look it up again.
    fake_twitch.requests.clear()
    twitch.poll_once(db, tclient, game_id)
    assert not any(p == "/helix/games" for p, _ in fake_twitch.requests)


def test_no_live_streams_is_a_real_zero(db, game_id, tclient, fake_twitch):
    assert twitch.poll_once(db, tclient, game_id) == "ok"
    snap = snapshots(db, game_id)[0]
    assert (snap["live_channels"], snap["total_viewers"]) == (0, 0)


def test_unknown_category_is_unavailable_not_zero(db, game_id, tclient, fake_twitch):
    fake_twitch.categories = {}
    assert twitch.poll_once(db, tclient, game_id) == "unavailable"
    assert snapshots(db, game_id) == []
    run = last_run(db, game_id)
    assert run["status"] == "unavailable" and "not found" in run["error"]


def test_failed_or_partial_walk_is_never_stored(db, game_id, tclient, fake_twitch):
    fake_twitch.streams = [stream(i, 10) for i in range(6)]
    fake_twitch.fail_streams_after_page = 1  # first page OK, second page fails after retries
    assert twitch.poll_once(db, tclient, game_id) == "error"
    assert snapshots(db, game_id) == []
    assert db.conn().execute("SELECT COUNT(*) FROM twitch_streams").fetchone()[0] == 0
    run = last_run(db, game_id)
    assert run["status"] == "error" and run["http_status"] == 500
    health = {h["source"]: h for h in store.source_health(db.conn(), game_id)}["twitch"]
    assert health["consecutive_failures"] == 1


def test_expired_token_is_refreshed_once(db, game_id, tclient, fake_twitch):
    fake_twitch.streams = [stream(1, 3)]
    assert twitch.poll_once(db, tclient, game_id) == "ok"
    fake_twitch.valid_tokens.clear()  # token revoked / expired server-side
    assert twitch.poll_once(db, tclient, game_id) == "ok"
    assert fake_twitch.tokens_issued == 2 and len(snapshots(db, game_id)) == 2


def test_rejected_credentials_are_reported_clearly(db, game_id, tclient, fake_twitch):
    fake_twitch.reject_credentials = True
    assert twitch.poll_once(db, tclient, game_id) == "error"
    assert "client credentials" in last_run(db, game_id)["error"]
    assert "secret" not in last_run(db, game_id)["error"].replace("client credentials", "")


def test_streams_moving_between_pages_are_deduplicated(db, game_id):
    conn = db.conn()
    dup = [stream(1, 50), stream(2, 30), stream(2, 30)]  # the same stream returned twice
    streams = list({s["id"]: s for s in dup}.values())
    with transaction(conn):
        store.insert_twitch_snapshot(conn, game_id, 1000, CAT, streams, 2)
    assert snapshots(db, game_id)[0]["total_viewers"] == 80


def test_category_id_lookup_and_per_stream_stats(db, game_id, tclient, fake_twitch):
    conn = db.conn()
    with transaction(conn):
        store.set_twitch_category(conn, game_id, CAT, None, None)  # configured by id
    for i, viewers in enumerate([10, 80, 30]):
        fake_twitch.streams = [stream(1, viewers, title=f"Launch day #{i}"), stream(2, 5, user=1)]
        assert twitch.poll_once(db, tclient, game_id) == "ok"
    s1 = dict(conn.execute("SELECT * FROM twitch_streams WHERE stream_id='s1'").fetchone())
    assert (s1["samples"], s1["peak_viewers"], s1["viewer_sum"], s1["title"]) == (3, 80, 120, "Launch day #2")
    assert s1["started_at"] == int(datetime.fromisoformat("2026-10-15T16:00:00+00:00").timestamp())
    # Two streams from one user count as one live channel.
    assert snapshots(db, game_id)[0]["live_channels"] == 1


# -- aggregates -------------------------------------------------------------------------------------

def test_twitch_buckets_have_coverage_gaps_and_viewer_hours():
    start = int(datetime(2026, 10, 15, 18, 0, tzinfo=WAW).timestamp())
    snaps = [(start + i * 60, 100, 2) for i in range(5)] + [(start + 600, 40, 1)]
    obs = [(start + i * 60, "s1", "u1") for i in range(5)] + [(start + i * 60, "s2", "u2") for i in range(5)] + \
          [(start + 600, "s3", "u1")]
    rows = aggregates.compute_twitch_buckets(snaps, obs, "5m", WAW, 60, start, start + 900)
    assert [r["samples"] for r in rows] == [5, 0, 1]
    assert rows[0]["viewer_hours"] == round(500 * 60 / 3600, 2)
    assert (rows[0]["unique_streams"], rows[0]["unique_channels"], rows[0]["max_channels"]) == (2, 2, 2)
    assert rows[1]["mean_viewers"] is None and rows[1]["coverage"] == 0.0  # gap, not zero
    assert rows[2]["coverage"] == 0.2 and rows[2]["unique_channels"] == 1


# -- API & exports ------------------------------------------------------------------------------------

B = 1_790_000_000


def seed(db, gid):
    conn = db.conn()
    with transaction(conn):
        conn.execute("UPDATE games SET twitch_monitoring_started_at=?, twitch_category_id=?, twitch_category_name=? "
                     "WHERE id=?", (B, CAT, "Wild West Pioneers", gid))
        store.insert_twitch_snapshot(conn, gid, B + 60, CAT, [stream(1, 100), stream(2, 20, title="=HYPERLINK(1)")], 1)
        store.insert_twitch_snapshot(conn, gid, B + 120, CAT, [stream(1, 150)], 1)
        store.insert_twitch_snapshot(conn, gid, B + 3000, CAT, [], 1)


def test_twitch_api_endpoints(settings, db, game_id):
    seed(db, game_id)
    db.close()
    app = create_app(settings, start_collector=False)
    with TestClient(app) as c:
        st = c.get("/api/status").json()
        assert st["twitch"]["available"] is False and st["twitch"]["category_id"] == CAT
        ov = c.get(f"/api/twitch/overview?from={B}&to={B + 4000}").json()
        assert ov["peak_viewers"]["total_viewers"] == 150 and ov["peak_channels"]["live_channels"] == 2
        assert ov["current"]["total_viewers"] == 0 and ov["stale"] is True and ov["live"] == []
        r = ov["range"]
        assert (r["samples"], r["unique_streams"], r["unique_channels"], r["peak_viewers"]) == (3, 2, 2, 150)
        assert r["viewer_hours"] == round(270 * 60 / 3600, 1)
        assert [t["stream_id"] for t in ov["top_streams"]] == ["s1", "s2"]
        assert ov["top_streams"][0]["url"] == "https://www.twitch.tv/streamer1"
        series = c.get(f"/api/twitch/series?from={B}&to={B + 4000}").json()
        assert series["resolution"] == "raw" and None in series["viewers"]  # gap marker between 120 and 3000
        assert c.put("/api/settings", json={"twitch_category": "Some Other Game"}).status_code == 200
        st = c.get("/api/status").json()["twitch"]
        assert st["category_query"] == "Some Other Game" and st["category_id"] is None
        for ds in ("twitch_snapshots", "twitch_streams", "twitch_stream_observations", "twitch_aggregates"):
            assert c.get(f"/api/export/{ds}").status_code == 200


def test_twitch_exports_and_zip(settings, db, game_id, tmp_path):
    seed(db, game_id)
    conn = db.conn()
    aggregates.rebuild_twitch(conn, game_id, "Europe/Warsaw", 60, until=B + 3600)
    cols, rows = exports.twitch_streams(conn, game_id, B + 100, B + 200)
    assert [r[0] for r in rows] == ["s1"]  # s2 was last seen at B+60, before the range
    body = exports.write_csv(cols, rows)
    cols, rows = exports.twitch_streams(conn, game_id, None, None)
    data = list(csv.DictReader(io.StringIO(exports.write_csv(cols, rows).decode("utf-8-sig"), newline="")))
    assert {d["stream_id"]: d["title"] for d in data}["s2"] == "'=HYPERLINK(1)"  # formula injection neutralised
    assert body.startswith(b"\xef\xbb\xbf")
    dest = tmp_path / "out.zip"
    exports.build_zip(conn, game_id, None, None, settings, dest)
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        meta = json.loads(zf.read("metadata.json"))
    assert {"twitch_snapshots.csv", "twitch_streams.csv", "twitch_stream_observations.csv",
            "twitch_aggregates.csv"} <= names
    assert meta["twitch"]["snapshot_count_total"] == 3 and meta["twitch"]["gap_count_in_range"] >= 1
    assert meta["datasets"]["twitch_snapshots"]["date_filter_field"] == "observed_at"


def test_twitch_secret_never_in_status_or_metadata(tmp_path, db, game_id):
    from wwp_radar.config import Settings
    s = Settings(data_dir=tmp_path, twitch_client_id="cid", twitch_client_secret="topsecret-xyz")
    assert "topsecret" not in repr(s)
    meta = exports.metadata(db.conn(), game_id, None, None, s)
    assert "topsecret" not in json.dumps(meta) and meta["twitch"]["configured"] is True
