from datetime import datetime
from zoneinfo import ZoneInfo

from wwp_radar import aggregates, store
from wwp_radar.db import transaction

from .conftest import make_review

WAW = ZoneInfo("Europe/Warsaw")


def ts(s: str, tz=WAW) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=tz).timestamp())


def test_ccu_buckets_show_gaps_and_coverage():
    start = ts("2026-10-01T12:00")
    # 5 minutes at 60 s interval = 5 expected samples. Bucket 1 full, bucket 2 missing, bucket 3 partial.
    obs = [(start + i * 60, 100 + i) for i in range(5)] + [(start + 600 + 60, 50), (start + 600 + 120, 70)]
    rows = aggregates.compute_ccu_buckets(obs, "5m", WAW, 60, monitor_start=start, until=start + 900)
    assert [r["samples"] for r in rows] == [5, 0, 2]
    assert rows[0] == {**rows[0], "min_players": 100, "max_players": 104, "mean_players": 102.0, "coverage": 1.0}
    assert rows[1]["mean_players"] is None and rows[1]["coverage"] == 0.0  # gap: no interpolation
    assert rows[2]["coverage"] == 0.4 and rows[2]["mean_players"] == 60.0


def test_partial_bucket_coverage_uses_monitored_time_only():
    start = ts("2026-10-01T12:02")  # monitoring started mid-bucket
    obs = [(start + i * 60, 10) for i in range(3)]
    rows = aggregates.compute_ccu_buckets(obs, "5m", WAW, 60, monitor_start=start, until=start + 180)
    assert len(rows) == 1 and rows[0]["expected_samples"] == 3 and rows[0]["coverage"] == 1.0


def test_hourly_buckets_align_to_warsaw_hours():
    t = ts("2026-07-01T14:37")
    bs, be = aggregates.bucket_bounds(t, "1h", WAW)
    assert datetime.fromtimestamp(bs, WAW).strftime("%H:%M") == "14:00" and be - bs == 3600


def test_daily_buckets_use_warsaw_calendar_days_across_dst():
    # 2026-10-25: CEST -> CET, day lasts 25 hours.
    bs, be = aggregates.bucket_bounds(ts("2026-10-25T12:00"), "1d", WAW)
    assert be - bs == 25 * 3600
    assert datetime.fromtimestamp(bs, WAW).isoformat() == "2026-10-25T00:00:00+02:00"
    # 2026-03-29: CET -> CEST, 23 hours.
    bs, be = aggregates.bucket_bounds(ts("2026-03-29T12:00"), "1d", WAW)
    assert be - bs == 23 * 3600
    # 23:30 UTC on Oct 1 is already Oct 2 in Warsaw.
    utc = ZoneInfo("UTC")
    bs, _ = aggregates.bucket_bounds(ts("2026-10-01T23:30", utc), "1d", WAW)
    assert datetime.fromtimestamp(bs, WAW).date().isoformat() == "2026-10-02"


def test_daily_ccu_coverage_on_25h_day():
    start = ts("2026-10-25T00:00")
    rows = aggregates.compute_ccu_buckets([(start, 1)], "1d", WAW, 60, monitor_start=start, until=start + 25 * 3600)
    assert rows[0]["expected_samples"] == 1500


def test_review_aggregates_by_creation_time_and_separate_sentiment_changes(db, game_id):
    conn = db.conn()
    created = ts("2026-10-01T10:15")
    import_time = ts("2026-10-05T09:00")
    with transaction(conn):
        store.upsert_review(conn, game_id, make_review(1, created, up=True), import_time)
        store.upsert_review(conn, game_id, make_review(2, created + 60, up=False, steam=False), import_time)
        edit_time = ts("2026-10-06T18:10")
        store.upsert_review(conn, game_id, make_review(1, created, up=False, text="changed", updated=edit_time), edit_time + 30)
    aggregates.rebuild_reviews(conn, game_id, "Europe/Warsaw")
    rows = {(r["bucket"], r["bucket_start"]): dict(r) for r in conn.execute("SELECT * FROM review_aggregates")}
    h = rows[("1h", ts("2026-10-01T10:00"))]
    # Both counted at creation hour (not import time), classified by current recommendation.
    assert (h["new_positive"], h["new_negative"], h["new_negative_steam"]) == (0, 2, 1)
    assert ("1h", ts("2026-10-05T09:00")) not in rows  # no burst at import time
    change = rows[("1h", ts("2026-10-06T18:00"))]
    assert change["changed_to_negative"] == 1 and change["new_negative"] == 0


def test_rebuild_ccu_is_idempotent(db, game_id):
    conn = db.conn()
    start = ts("2026-10-01T12:00")
    with transaction(conn):
        conn.execute("UPDATE games SET monitoring_started_at=? WHERE id=?", (start, game_id))
        for i in range(30):
            store.insert_ccu(conn, game_id, start + i * 60, i)
    aggregates.rebuild_ccu(conn, game_id, "Europe/Warsaw", 60, until=start + 1800)
    first = conn.execute("SELECT * FROM ccu_aggregates ORDER BY bucket, bucket_start").fetchall()
    aggregates.rebuild_ccu(conn, game_id, "Europe/Warsaw", 60, window_start=start + 600, until=start + 1800)
    second = conn.execute("SELECT * FROM ccu_aggregates ORDER BY bucket, bucket_start").fetchall()
    assert [tuple(r) for r in first] == [tuple(r) for r in second]
    assert conn.execute("SELECT COUNT(*) FROM ccu_observations").fetchone()[0] == 30  # raw data untouched
