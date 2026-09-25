"""Data-access functions shared by the collector, API, exports and tests."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from .db import now, transaction

# Fields whose change creates a new immutable review version. Volatile counters
# (votes, playtime_forever, comment_count) are updated in place without a version.
VERSIONED_FIELDS = (
    "review_text",
    "voted_up",
    "language",
    "playtime_at_review",
    "steam_purchase",
    "received_for_free",
    "written_during_early_access",
    "developer_response",
)


# -- games & settings ------------------------------------------------------------

def ensure_game(conn: sqlite3.Connection, app_id: int, name: str) -> int:
    row = conn.execute("SELECT id FROM games WHERE app_id=?", (app_id,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO games(app_id, name, created_at) VALUES (?,?,?)", (app_id, name, now())
    )
    return cur.lastrowid


def get_game(conn: sqlite3.Connection, game_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()


def list_games(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM games ORDER BY id")]


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def active_game_id(conn: sqlite3.Connection) -> int | None:
    v = get_setting(conn, "active_game_id")
    return int(v) if v else None


def mark_monitoring_started(conn: sqlite3.Connection, game_id: int, ts: int) -> None:
    conn.execute(
        "UPDATE games SET monitoring_started_at=? WHERE id=? AND monitoring_started_at IS NULL", (ts, game_id)
    )


def mark_twitch_started(conn: sqlite3.Connection, game_id: int, ts: int) -> None:
    conn.execute(
        "UPDATE games SET twitch_monitoring_started_at=? WHERE id=? AND twitch_monitoring_started_at IS NULL",
        (ts, game_id),
    )


def set_twitch_category(conn: sqlite3.Connection, game_id: int, query: str | None, category_id: str | None,
                        category_name: str | None) -> None:
    conn.execute(
        "UPDATE games SET twitch_category=?, twitch_category_id=?, twitch_category_name=? WHERE id=?",
        (query, category_id, category_name, game_id),
    )


# -- checkpoints -------------------------------------------------------------------

def get_checkpoint(conn: sqlite3.Connection, game_id: int, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM checkpoints WHERE game_id=? AND key=?", (game_id, key)).fetchone()
    return json.loads(row["value"]) if row else default


def set_checkpoint(conn: sqlite3.Connection, game_id: int, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO checkpoints(game_id, key, value, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(game_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (game_id, key, json.dumps(value), now()),
    )


# -- collection runs & health ------------------------------------------------------

def record_run(
    conn: sqlite3.Connection,
    game_id: int,
    source: str,
    started_at: int,
    status: str,
    *,
    attempts: int = 1,
    http_status: int | None = None,
    items: int | None = None,
    error: str | None = None,
    finished_at: int | None = None,
) -> None:
    """Log a collection attempt and update per-source health. Call inside a transaction."""
    finished = finished_at if finished_at is not None else now()
    conn.execute(
        "INSERT INTO collection_runs(game_id, source, started_at, finished_at, status, attempts, http_status, items, error) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (game_id, source, started_at, finished, status, attempts, http_status, items, error),
    )
    conn.execute(
        "INSERT INTO source_health(game_id, source) VALUES (?,?) ON CONFLICT DO NOTHING", (game_id, source)
    )
    if status == "ok":
        conn.execute(
            "UPDATE source_health SET last_attempt_at=?, last_success_at=?, consecutive_failures=0 "
            "WHERE game_id=? AND source=?",
            (finished, finished, game_id, source),
        )
    else:
        conn.execute(
            "UPDATE source_health SET last_attempt_at=?, last_error_at=?, last_error=?, "
            "consecutive_failures=consecutive_failures+1 WHERE game_id=? AND source=?",
            (finished, finished, f"[{status}] {error or ''}".strip(), game_id, source),
        )


def source_health(conn: sqlite3.Connection, game_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM source_health WHERE game_id=? ORDER BY source", (game_id,))]


# -- CCU -----------------------------------------------------------------------------

def insert_ccu(conn: sqlite3.Connection, game_id: int, observed_at: int, player_count: int) -> None:
    if not isinstance(player_count, int) or player_count < 0:
        raise ValueError("player_count must be a non-negative int from a successful response")
    conn.execute(
        "INSERT OR IGNORE INTO ccu_observations(game_id, observed_at, player_count) VALUES (?,?,?)",
        (game_id, observed_at, player_count),
    )


# -- Twitch --------------------------------------------------------------------------

def parse_iso(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def insert_twitch_snapshot(conn: sqlite3.Connection, game_id: int, observed_at: int, category_id: str,
                           streams: list[dict], pages: int) -> int | None:
    """Store one complete category snapshot and update per-stream stats. Call inside a transaction.

    Returns None (and stores nothing) if a snapshot already exists for that second.
    """
    counts = [max(0, int(s.get("viewer_count") or 0)) for s in streams]
    cur = conn.execute(
        "INSERT OR IGNORE INTO twitch_snapshots(game_id, observed_at, category_id, live_channels, total_viewers, pages) "
        "VALUES (?,?,?,?,?,?)",
        (game_id, observed_at, category_id, len({str(s["user_id"]) for s in streams}), sum(counts), pages),
    )
    if cur.rowcount == 0:
        return None
    snap_id = cur.lastrowid
    for s, viewers in zip(streams, counts):
        sid = str(s["id"])
        conn.execute(
            "INSERT INTO twitch_stream_observations(snapshot_id, game_id, stream_id, observed_at, viewer_count) "
            "VALUES (?,?,?,?,?)",
            (snap_id, game_id, sid, observed_at, viewers),
        )
        conn.execute(
            "INSERT INTO twitch_streams(game_id, stream_id, user_id, user_login, user_name, category_id, title, language, "
            "is_mature, tags, started_at, first_seen_at, last_seen_at, samples, viewer_sum, peak_viewers, peak_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(game_id, stream_id) DO UPDATE SET user_login=excluded.user_login, user_name=excluded.user_name, "
            "title=excluded.title, language=excluded.language, is_mature=excluded.is_mature, tags=excluded.tags, "
            "last_seen_at=excluded.last_seen_at, samples=samples+1, viewer_sum=viewer_sum+excluded.viewer_sum, "
            "peak_at=CASE WHEN excluded.peak_viewers > peak_viewers THEN excluded.peak_at ELSE peak_at END, "
            "peak_viewers=MAX(peak_viewers, excluded.peak_viewers)",
            (game_id, sid, str(s["user_id"]), str(s.get("user_login") or ""), s.get("user_name"), category_id,
             s.get("title"), s.get("language"), _b(s.get("is_mature")),
             json.dumps(s.get("tags") or [], ensure_ascii=False), parse_iso(s.get("started_at")),
             observed_at, observed_at, viewers, viewers, observed_at),
        )
    return snap_id


# -- reviews -------------------------------------------------------------------------

def _b(v: Any) -> int | None:
    if v is None:
        return None
    return 1 if v else 0


def _i(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def normalize_review(raw: dict) -> dict:
    author = raw.get("author") or {}
    return {
        "recommendation_id": str(raw["recommendationid"]),
        "author_steamid": str(author.get("steamid")) if author.get("steamid") else None,
        "language": raw.get("language"),
        "review_text": raw.get("review") or "",
        "voted_up": 1 if raw.get("voted_up") else 0,
        "timestamp_created": int(raw["timestamp_created"]),
        "timestamp_updated": _i(raw.get("timestamp_updated")),
        "playtime_at_review": _i(author.get("playtime_at_review")),
        "playtime_forever": _i(author.get("playtime_forever")),
        "steam_purchase": _b(raw.get("steam_purchase")),
        "received_for_free": _b(raw.get("received_for_free")),
        "refunded": _b(raw.get("refunded")),
        "written_during_early_access": _b(raw.get("written_during_early_access")),
        "primarily_steam_deck": _b(raw.get("primarily_steam_deck")),
        "votes_up": _i(raw.get("votes_up")),
        "votes_funny": _i(raw.get("votes_funny")),
        "weighted_vote_score": float(raw["weighted_vote_score"]) if raw.get("weighted_vote_score") not in (None, "") else None,
        "comment_count": _i(raw.get("comment_count")),
        "developer_response": raw.get("developer_response") or None,
        "timestamp_dev_responded": _i(raw.get("timestamp_dev_responded")),
    }


def content_hash(r: dict) -> str:
    payload = json.dumps({k: r.get(k) for k in VERSIONED_FIELDS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upsert_review(conn: sqlite3.Connection, game_id: int, raw: dict, observed_at: int) -> str:
    """Insert/update one review. Returns 'new', 'changed' or 'unchanged'. Call inside a transaction."""
    r = normalize_review(raw)
    h = content_hash(r)
    rid = r["recommendation_id"]
    existing = conn.execute(
        "SELECT content_hash, voted_up, version_count FROM reviews WHERE game_id=? AND recommendation_id=?",
        (game_id, rid),
    ).fetchone()
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)

    if existing is None:
        cols = list(r.keys()) + ["content_hash", "version_count", "first_seen_at", "last_seen_at", "game_id"]
        vals = list(r.values()) + [h, 1, observed_at, observed_at, game_id]
        conn.execute(
            f"INSERT INTO reviews({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
        )
        _insert_version(conn, game_id, r, 1, observed_at, h, 0, raw_json)
        return "new"

    volatile = (
        "timestamp_updated", "playtime_forever", "refunded", "primarily_steam_deck", "votes_up",
        "votes_funny", "weighted_vote_score", "comment_count", "timestamp_dev_responded", "author_steamid",
    )
    if existing["content_hash"] == h:
        sets = ", ".join(f"{k}=?" for k in volatile)
        conn.execute(
            f"UPDATE reviews SET {sets}, last_seen_at=? WHERE game_id=? AND recommendation_id=?",
            [r[k] for k in volatile] + [observed_at, game_id, rid],
        )
        return "unchanged"

    version_no = existing["version_count"] + 1
    sentiment_changed = 1 if existing["voted_up"] != r["voted_up"] else 0
    _insert_version(conn, game_id, r, version_no, observed_at, h, sentiment_changed, raw_json)
    fields = [k for k in r.keys() if k not in ("recommendation_id", "timestamp_created")]
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE reviews SET {sets}, content_hash=?, version_count=?, last_seen_at=? "
        "WHERE game_id=? AND recommendation_id=?",
        [r[k] for k in fields] + [h, version_no, observed_at, game_id, rid],
    )
    return "changed"


def _insert_version(conn, game_id, r, version_no, observed_at, h, sentiment_changed, raw_json):
    conn.execute(
        "INSERT INTO review_versions(game_id, recommendation_id, version_no, observed_at, timestamp_created, "
        "timestamp_updated, voted_up, review_text, language, playtime_at_review, steam_purchase, received_for_free, "
        "written_during_early_access, developer_response, timestamp_dev_responded, content_hash, sentiment_changed, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            game_id, r["recommendation_id"], version_no, observed_at, r["timestamp_created"], r["timestamp_updated"],
            r["voted_up"], r["review_text"], r["language"], r["playtime_at_review"], r["steam_purchase"],
            r["received_for_free"], r["written_during_early_access"], r["developer_response"],
            r["timestamp_dev_responded"], h, sentiment_changed, raw_json,
        ),
    )


def insert_summary(conn: sqlite3.Connection, game_id: int, observed_at: int, population: str,
                   query_params: dict, qs: dict) -> None:
    conn.execute(
        "INSERT INTO review_summary_snapshots(game_id, observed_at, population, query_params, review_score, "
        "review_score_desc, total_positive, total_negative, total_reviews) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            game_id, observed_at, population, json.dumps(query_params, sort_keys=True),
            _i(qs.get("review_score")), qs.get("review_score_desc"),
            int(qs["total_positive"]), int(qs["total_negative"]), int(qs["total_reviews"]),
        ),
    )


# -- annotations -----------------------------------------------------------------------

ANNOTATION_KINDS = ("launch", "hotfix", "patch", "stream", "marketing", "other")


def list_annotations(conn, game_id: int, start: int | None = None, end: int | None = None) -> list[dict]:
    q = "SELECT * FROM annotations WHERE game_id=?"
    args: list[Any] = [game_id]
    if start is not None:
        q += " AND event_at>=?"
        args.append(start)
    if end is not None:
        q += " AND event_at<?"
        args.append(end)
    return [dict(r) for r in conn.execute(q + " ORDER BY event_at", args)]


def create_annotation(conn, game_id: int, event_at: int, kind: str, title: str, description: str | None) -> int:
    ts = now()
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO annotations(game_id, event_at, kind, title, description, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (game_id, event_at, kind, title, description, ts, ts),
        )
    return cur.lastrowid


def update_annotation(conn, ann_id: int, game_id: int, event_at: int, kind: str, title: str, description: str | None) -> bool:
    with transaction(conn):
        cur = conn.execute(
            "UPDATE annotations SET event_at=?, kind=?, title=?, description=?, updated_at=? WHERE id=? AND game_id=?",
            (event_at, kind, title, description, now(), ann_id, game_id),
        )
    return cur.rowcount > 0


def delete_annotation(conn, ann_id: int, game_id: int) -> bool:
    with transaction(conn):
        cur = conn.execute("DELETE FROM annotations WHERE id=? AND game_id=?", (ann_id, game_id))
    return cur.rowcount > 0
