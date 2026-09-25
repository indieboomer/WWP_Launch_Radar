"""Rebuildable aggregates derived from raw observations.

Semantics (also documented in README):
* 5m / 1h buckets are aligned to UTC epoch multiples. Because Europe/Warsaw offsets are whole
  hours, hourly buckets coincide with local clock hours.
* 1d buckets are calendar days in the display timezone (23 h / 25 h on DST change days).
* CCU buckets exist for every bucket from monitoring start until now, including buckets with no
  samples (min/max/mean NULL). coverage = samples / expected_samples (capped at 1), where
  expected_samples = monitored seconds inside the bucket / poll interval. Missing samples are
  never interpolated.
* Review buckets count reviews by their Steam creation time (timestamp_created), classified by
  their *current* recommendation; sentiment changes are counted separately at the edit time
  (timestamp_updated of the changed version). Only non-empty review buckets are stored.
* Summary buckets hold the last Steam summary snapshot observed within the bucket, per population.
* Twitch buckets follow the CCU rules (every bucket since Twitch monitoring started, coverage,
  no interpolation) over complete category snapshots. viewer_hours is an estimate:
  sum(total_viewers of each snapshot) * poll interval / 3600, so it undercounts when coverage < 1.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from .db import now, transaction

FIXED_BUCKETS = {"5m": 300, "1h": 3600}


def local_day_start(ts: int, tz: ZoneInfo) -> int:
    d = datetime.fromtimestamp(ts, tz).date()
    return int(datetime.combine(d, dtime(0), tzinfo=tz).timestamp())


def local_day_bounds(ts: int, tz: ZoneInfo) -> tuple[int, int]:
    d = datetime.fromtimestamp(ts, tz).date()
    start = datetime.combine(d, dtime(0), tzinfo=tz)
    end = datetime.combine(d + timedelta(days=1), dtime(0), tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def bucket_bounds(ts: int, bucket: str, tz: ZoneInfo) -> tuple[int, int]:
    if bucket in FIXED_BUCKETS:
        size = FIXED_BUCKETS[bucket]
        start = ts - ts % size
        return start, start + size
    if bucket == "1d":
        return local_day_bounds(ts, tz)
    raise ValueError(bucket)


def iter_buckets(start: int, end: int, bucket: str, tz: ZoneInfo) -> Iterable[tuple[int, int]]:
    """Buckets overlapping [start, end)."""
    if start >= end:
        return
    bs, be = bucket_bounds(start, bucket, tz)
    while bs < end:
        yield bs, be
        bs, be = bucket_bounds(be, bucket, tz)


# -- CCU ---------------------------------------------------------------------------------

def compute_ccu_buckets(observations: list[tuple[int, int]], bucket: str, tz: ZoneInfo, interval: int,
                        monitor_start: int, until: int, window_start: int | None = None) -> list[dict]:
    """observations: sorted (observed_at, player_count). Returns bucket dicts in [window_start, until)."""
    lo = monitor_start if window_start is None else max(monitor_start, window_start)
    by_bucket: dict[int, list[int]] = defaultdict(list)
    for ts, count in observations:
        if ts < lo or ts >= until:
            continue
        bs, _ = bucket_bounds(ts, bucket, tz)
        by_bucket[bs].append(count)
    out = []
    for bs, be in iter_buckets(lo, until, bucket, tz):
        values = by_bucket.get(bs, [])
        monitored = min(be, until) - max(bs, monitor_start)
        expected = max(monitored / interval, 1.0) if monitored > 0 else 0.0
        samples = len(values)
        out.append({
            "bucket": bucket,
            "bucket_start": bs,
            "bucket_end": be,
            "samples": samples,
            "expected_samples": round(expected, 3),
            "coverage": round(min(1.0, samples / expected), 4) if expected else 0.0,
            "min_players": min(values) if values else None,
            "max_players": max(values) if values else None,
            "mean_players": round(sum(values) / samples, 2) if values else None,
        })
    return out


def _monitor_start(conn: sqlite3.Connection, game_id: int) -> int | None:
    row = conn.execute(
        "SELECT MIN(x) FROM (SELECT monitoring_started_at AS x FROM games WHERE id=? "
        "UNION ALL SELECT MIN(observed_at) FROM ccu_observations WHERE game_id=?)",
        (game_id, game_id),
    ).fetchone()
    return row[0]


def rebuild_ccu(conn: sqlite3.Connection, game_id: int, tz_name: str, interval: int,
                window_start: int | None = None, until: int | None = None) -> None:
    tz = ZoneInfo(tz_name)
    start = _monitor_start(conn, game_id)
    if start is None:
        return
    until = until or now()
    for bucket in ("5m", "1h", "1d"):
        # Always recompute whole buckets: align the window to the start of its first bucket.
        lo = bucket_bounds(max(window_start or start, start), bucket, tz)[0]
        obs = conn.execute(
            "SELECT observed_at, player_count FROM ccu_observations WHERE game_id=? AND observed_at>=? AND observed_at<? "
            "ORDER BY observed_at",
            (game_id, lo, until),
        ).fetchall()
        rows = compute_ccu_buckets([(r[0], r[1]) for r in obs], bucket, tz, interval, start, until, lo)
        with transaction(conn):
            conn.execute("DELETE FROM ccu_aggregates WHERE game_id=? AND bucket=? AND bucket_start>=?", (game_id, bucket, lo))
            conn.executemany(
                "INSERT INTO ccu_aggregates(game_id, bucket, bucket_start, bucket_end, samples, expected_samples, coverage, "
                "min_players, max_players, mean_players) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(game_id, r["bucket"], r["bucket_start"], r["bucket_end"], r["samples"], r["expected_samples"],
                  r["coverage"], r["min_players"], r["max_players"], r["mean_players"]) for r in rows],
            )


# -- Twitch --------------------------------------------------------------------------------

def compute_twitch_buckets(snapshots: list[tuple[int, int, int]], streams: list[tuple[int, str, str]], bucket: str,
                           tz: ZoneInfo, interval: int, monitor_start: int, until: int,
                           window_start: int | None = None) -> list[dict]:
    """snapshots: (observed_at, total_viewers, live_channels); streams: (observed_at, stream_id, user_id)."""
    lo = monitor_start if window_start is None else max(monitor_start, window_start)
    snaps: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for ts, viewers, channels in snapshots:
        if lo <= ts < until:
            snaps[bucket_bounds(ts, bucket, tz)[0]].append((viewers, channels))
    ids: dict[int, tuple[set, set]] = defaultdict(lambda: (set(), set()))
    for ts, stream_id, user_id in streams:
        if lo <= ts < until:
            s_ids, u_ids = ids[bucket_bounds(ts, bucket, tz)[0]]
            s_ids.add(stream_id)
            u_ids.add(user_id)
    out = []
    for bs, be in iter_buckets(lo, until, bucket, tz):
        values = snaps.get(bs, [])
        viewers = [v for v, _ in values]
        channels = [c for _, c in values]
        monitored = min(be, until) - max(bs, monitor_start)
        expected = max(monitored / interval, 1.0) if monitored > 0 else 0.0
        s_ids, u_ids = ids.get(bs, (set(), set()))
        out.append({
            "bucket": bucket,
            "bucket_start": bs,
            "bucket_end": be,
            "samples": len(values),
            "expected_samples": round(expected, 3),
            "coverage": round(min(1.0, len(values) / expected), 4) if expected else 0.0,
            "min_viewers": min(viewers) if viewers else None,
            "max_viewers": max(viewers) if viewers else None,
            "mean_viewers": round(sum(viewers) / len(viewers), 2) if viewers else None,
            "max_channels": max(channels) if channels else None,
            "mean_channels": round(sum(channels) / len(channels), 2) if channels else None,
            "unique_streams": len(s_ids),
            "unique_channels": len(u_ids),
            "viewer_hours": round(sum(viewers) * interval / 3600, 2),
        })
    return out


TWITCH_AGG_COLUMNS = ("bucket", "bucket_start", "bucket_end", "samples", "expected_samples", "coverage", "min_viewers",
                      "max_viewers", "mean_viewers", "max_channels", "mean_channels", "unique_streams",
                      "unique_channels", "viewer_hours")


def rebuild_twitch(conn: sqlite3.Connection, game_id: int, tz_name: str, interval: int,
                   window_start: int | None = None, until: int | None = None) -> None:
    tz = ZoneInfo(tz_name)
    start = conn.execute(
        "SELECT MIN(x) FROM (SELECT twitch_monitoring_started_at AS x FROM games WHERE id=? "
        "UNION ALL SELECT MIN(observed_at) FROM twitch_snapshots WHERE game_id=?)",
        (game_id, game_id),
    ).fetchone()[0]
    if start is None:
        return
    until = until or now()
    for bucket in ("5m", "1h", "1d"):
        lo = bucket_bounds(max(window_start or start, start), bucket, tz)[0]
        snaps = conn.execute(
            "SELECT observed_at, total_viewers, live_channels FROM twitch_snapshots "
            "WHERE game_id=? AND observed_at>=? AND observed_at<? ORDER BY observed_at", (game_id, lo, until)).fetchall()
        streams = conn.execute(
            "SELECT o.observed_at, o.stream_id, s.user_id FROM twitch_stream_observations o "
            "JOIN twitch_streams s ON s.game_id=o.game_id AND s.stream_id=o.stream_id "
            "WHERE o.game_id=? AND o.observed_at>=? AND o.observed_at<?", (game_id, lo, until)).fetchall()
        rows = compute_twitch_buckets([tuple(r) for r in snaps], [tuple(r) for r in streams], bucket, tz, interval,
                                      start, until, lo)
        with transaction(conn):
            conn.execute("DELETE FROM twitch_aggregates WHERE game_id=? AND bucket=? AND bucket_start>=?",
                         (game_id, bucket, lo))
            conn.executemany(
                f"INSERT INTO twitch_aggregates(game_id, {', '.join(TWITCH_AGG_COLUMNS)}) "
                f"VALUES ({', '.join('?' * (len(TWITCH_AGG_COLUMNS) + 1))})",
                [(game_id, *(r[k] for k in TWITCH_AGG_COLUMNS)) for r in rows],
            )


# -- reviews -------------------------------------------------------------------------------

def rebuild_reviews(conn: sqlite3.Connection, game_id: int, tz_name: str) -> None:
    tz = ZoneInfo(tz_name)
    reviews = conn.execute(
        "SELECT timestamp_created, voted_up, steam_purchase FROM reviews WHERE game_id=?", (game_id,)
    ).fetchall()
    changes = conn.execute(
        "SELECT COALESCE(timestamp_updated, observed_at) AS t, voted_up FROM review_versions "
        "WHERE game_id=? AND sentiment_changed=1",
        (game_id,),
    ).fetchall()
    rows = []
    for bucket in ("1h", "1d"):
        acc: dict[tuple[int, int], dict] = {}

        def slot(ts: int) -> dict:
            bs, be = bucket_bounds(ts, bucket, tz)
            return acc.setdefault((bs, be), defaultdict(int))

        for r in reviews:
            s = slot(r["timestamp_created"])
            pos = r["voted_up"] == 1
            s["new_positive" if pos else "new_negative"] += 1
            if r["steam_purchase"] == 1:
                s["new_positive_steam" if pos else "new_negative_steam"] += 1
        for c in changes:
            s = slot(c["t"])
            s["changed_to_positive" if c["voted_up"] == 1 else "changed_to_negative"] += 1
        for (bs, be), s in acc.items():
            rows.append((game_id, bucket, bs, be, s["new_positive"], s["new_negative"], s["new_positive_steam"],
                         s["new_negative_steam"], s["changed_to_positive"], s["changed_to_negative"]))
    with transaction(conn):
        conn.execute("DELETE FROM review_aggregates WHERE game_id=?", (game_id,))
        conn.executemany("INSERT INTO review_aggregates VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def rebuild_summaries(conn: sqlite3.Connection, game_id: int, tz_name: str) -> None:
    tz = ZoneInfo(tz_name)
    snaps = conn.execute(
        "SELECT observed_at, population, total_positive, total_negative, total_reviews FROM review_summary_snapshots "
        "WHERE game_id=? ORDER BY observed_at",
        (game_id,),
    ).fetchall()
    rows = []
    for bucket in ("1h", "1d"):
        acc: dict[tuple[str, int], dict] = {}
        for s in snaps:
            bs, be = bucket_bounds(s["observed_at"], bucket, tz)
            a = acc.setdefault((s["population"], bs), {"be": be, "n": 0})
            a["n"] += 1
            a["last"] = s  # ordered by time -> last wins
        for (pop, bs), a in acc.items():
            s = a["last"]
            voted = s["total_positive"] + s["total_negative"]
            pct = round(100.0 * s["total_positive"] / voted, 2) if voted else None
            rows.append((game_id, bucket, pop, bs, a["be"], a["n"], s["observed_at"], s["total_positive"],
                         s["total_negative"], s["total_reviews"], pct))
    with transaction(conn):
        conn.execute("DELETE FROM summary_aggregates WHERE game_id=?", (game_id,))
        conn.executemany("INSERT INTO summary_aggregates VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def rebuild_all(conn: sqlite3.Connection, game_id: int, tz_name: str, interval: int, twitch_interval: int = 60) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM ccu_aggregates WHERE game_id=?", (game_id,))
        conn.execute("DELETE FROM twitch_aggregates WHERE game_id=?", (game_id,))
    rebuild_ccu(conn, game_id, tz_name, interval)
    rebuild_reviews(conn, game_id, tz_name)
    rebuild_summaries(conn, game_id, tz_name)
    rebuild_twitch(conn, game_id, tz_name, twitch_interval)


def refresh_recent(conn: sqlite3.Connection, game_id: int, tz_name: str, interval: int, twitch_interval: int = 60) -> None:
    rebuild_ccu(conn, game_id, tz_name, interval, window_start=now() - 2 * 86400)
    rebuild_reviews(conn, game_id, tz_name)
    rebuild_summaries(conn, game_id, tz_name)
    rebuild_twitch(conn, game_id, tz_name, twitch_interval, window_start=now() - 2 * 86400)

