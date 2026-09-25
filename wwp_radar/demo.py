"""Deterministic demo data, written ONLY to the separate demo database (demo.sqlite3).

The real database (radar.sqlite3) is never touched. The dashboard shows a DEMO banner when
started with WWP_DEMO=1.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

from . import aggregates, store
from .collector import SUMMARY_POPULATIONS
from .config import Settings
from .db import Database, now, transaction

SAMPLE_TEXTS = [
    ("english", True, "Great colony builder, the western vibe is spot on. Needs more content but solid EA start."),
    ("english", False, "Crashed three times in two hours and my save got corrupted. Refunding until fixed."),
    ("polish", True, "Świetny klimat Dzikiego Zachodu, budowanie miasteczka wciąga."),
    ("polish", False, "Samouczek nic nie tłumaczy, a interfejs jest nieczytelny."),
    ("german", True, "Macht Spaß, die Wirtschaft ist interessant."),
    ("schinese", False, "优化太差，帧数很低。"),
    ("english", True, "=HYPERLINK(\"http://example.com\") formula-looking text to test export safety"),
    ("english", False, "Balance is off - bandits are way too strong in the early game.\nSecond line of text."),
]


def generate(settings: Settings, hours: int = 48, seed: int = 7) -> str:
    assert settings.demo, "demo data must go to the demo database"
    rng = random.Random(seed)
    db = Database(settings.db_path)
    conn = db.conn()
    end = now() - now() % 60
    start = end - hours * 3600
    with transaction(conn):
        gid = store.ensure_game(conn, settings.app_id, settings.game_name + " (DEMO)")
        store.set_setting(conn, "active_game_id", str(gid))
        conn.execute("UPDATE games SET launch_at=?, monitoring_started_at=? WHERE id=?", (start + 3600 * 2, start, gid))
        for table in ("twitch_stream_observations", "twitch_snapshots", "twitch_streams", "ccu_observations", "reviews", "review_versions", "review_summary_snapshots", "annotations",
                      "collection_runs", "checkpoints"):
            conn.execute(f"DELETE FROM {table} WHERE game_id=?", (gid,))
        t = start
        while t < end:
            h = (t - start) / 3600
            # Simulated launch ramp with a daily cycle; a 40-minute outage creates a visible gap.
            if not (start + 20 * 3600 <= t < start + 20 * 3600 + 2400):
                base = 4000 * (1 - math.exp(-max(0, h - 2) / 3)) * (0.75 + 0.25 * math.sin((h - 6) / 24 * 2 * math.pi))
                store.insert_ccu(conn, gid, t + rng.randint(0, 3), max(0, int(base + rng.gauss(0, 40))))
            t += settings.ccu_interval
        pos = neg = pos_s = neg_s = 0
        for i in range(600):
            created = start + 7200 + int(rng.random() ** 1.5 * (end - start - 7200))
            lang, up, text = SAMPLE_TEXTS[i % len(SAMPLE_TEXTS)]
            up = up if rng.random() > 0.15 else not up
            steam = rng.random() > 0.1
            raw = {
                "recommendationid": str(900000 + i), "author": {"steamid": str(76561190000000000 + i),
                "playtime_at_review": rng.randint(10, 900), "playtime_forever": rng.randint(10, 1500)},
                "language": lang, "review": text, "timestamp_created": created, "timestamp_updated": created,
                "voted_up": up, "votes_up": rng.randint(0, 30), "votes_funny": 0, "weighted_vote_score": "0.5",
                "comment_count": 0, "steam_purchase": steam, "received_for_free": not steam, "refunded": False,
                "written_during_early_access": True, "primarily_steam_deck": False,
            }
            store.upsert_review(conn, gid, raw, min(end, created + rng.randint(30, 300)))
            if i % 40 == 0:  # some edited reviews, some with sentiment flips
                edited = dict(raw, review=text + " [EDIT: patched, better now]", voted_up=True if i % 80 == 0 else up,
                              timestamp_updated=min(end, created + 3600 * 5))
                store.upsert_review(conn, gid, edited, min(end, created + 3600 * 5 + 120))
            up_now = conn.execute("SELECT voted_up FROM reviews WHERE game_id=? AND recommendation_id=?",
                                  (gid, raw["recommendationid"])).fetchone()[0]
            pos += up_now
            neg += 1 - up_now
            pos_s += up_now if steam else 0
            neg_s += (1 - up_now) if steam else 0
        # Summary snapshots every 10 minutes (demo simplification of the 2-minute cadence).
        rows = conn.execute("SELECT timestamp_created, voted_up, steam_purchase FROM reviews WHERE game_id=?", (gid,)).fetchall()
        for t in range(start + 7200, end, 600):
            p = sum(1 for r in rows if r[0] <= t and r[1] == 1)
            n = sum(1 for r in rows if r[0] <= t and r[1] == 0)
            ps = sum(1 for r in rows if r[0] <= t and r[1] == 1 and r[2] == 1)
            ns = sum(1 for r in rows if r[0] <= t and r[1] == 0 and r[2] == 1)
            for pop, (a, b) in {"all_all": (p, n), "all_steam": (ps, ns)}.items():
                store.insert_summary(conn, gid, t, pop, SUMMARY_POPULATIONS[pop]["params"],
                                     {"total_positive": a, "total_negative": b, "total_reviews": a + b,
                                      "review_score": 6, "review_score_desc": "Mostly Positive"})
        store.set_checkpoint(conn, gid, "import", {"status": "complete", "started_at": start, "completed_at": start + 60,
                                                   "reviews_seen": 600, "expected_total": 600, "pages": 6})
        _twitch(conn, gid, rng, start + 7200, end, settings.twitch_interval, outage=(start + 20 * 3600, start + 20 * 3600 + 2400),
                big_stream_at=start + 30 * 3600)
        for src in ("ccu", "reviews_recent", "reviews_updated", "review_summary", "twitch"):
            store.record_run(conn, gid, src, end - 5, "ok", items=1, finished_at=end)
    for at, kind, title in ((start + 7200, "launch", "Premiera Early Access"),
                            (start + 26 * 3600, "hotfix", "Hotfix 0.1.1 - crash przy zapisie"),
                            (start + 30 * 3600, "stream", "Stream dewelopera")):
        store.create_annotation(conn, gid, at, kind, title, "Dane demonstracyjne")
    aggregates.rebuild_all(conn, gid, settings.display_tz, settings.ccu_interval, settings.twitch_interval)
    db.close()
    return str(settings.db_path)


STREAM_TITLES = ["Wild West Pioneers - pierwsze wrażenia!", "Building the perfect frontier town", "EA launch day stream",
                 "Budujemy miasteczko na Dzikim Zachodzie", "Chill colony building", "Bandits everywhere?! | !discord"]
STREAM_LANGS = ["en", "en", "en", "pl", "de", "ru", "es", "fr"]


def _twitch(conn, gid, rng: random.Random, start: int, end: int, interval: int, outage: tuple[int, int],
            big_stream_at: int) -> None:
    """Simulated Twitch category: many small streams plus one large streamer (matches the 'stream' annotation)."""
    conn.execute("UPDATE games SET twitch_monitoring_started_at=?, twitch_category_id=?, twitch_category_name=? WHERE id=?",
                 (start, "5550001", "Wild West Pioneers", gid))
    sessions = []
    for k in range(70):
        user = rng.randrange(28)
        s0 = start + int(rng.random() * (end - start))
        sessions.append({"id": f"demo{k}", "user": user, "from": s0, "to": s0 + rng.randint(3600, 5 * 3600),
                         "base": max(1, int(rng.lognormvariate(3.0, 1.1))), "title": rng.choice(STREAM_TITLES),
                         "lang": STREAM_LANGS[user % len(STREAM_LANGS)]})
    sessions.append({"id": "demo-big", "user": 99, "from": big_stream_at, "to": big_stream_at + 3 * 3600, "base": 4200,
                     "title": "DEV STREAM: Wild West Pioneers Q&A", "lang": "en"})
    t = start
    while t < end:
        if not (outage[0] <= t < outage[1]):
            live = []
            for x in sessions:
                if x["from"] <= t < x["to"]:
                    ramp = min(1.0, (t - x["from"]) / 900)
                    live.append({"id": x["id"], "user_id": f"demo-u{x['user']}", "user_login": f"demo_streamer{x['user']}",
                                 "user_name": f"DemoStreamer{x['user']}", "title": x["title"], "language": x["lang"],
                                 "viewer_count": max(0, int(x["base"] * ramp * rng.uniform(0.9, 1.1))),
                                 "started_at": datetime.fromtimestamp(x["from"], timezone.utc).isoformat(),
                                 "tags": [], "is_mature": False})
            store.insert_twitch_snapshot(conn, gid, t + rng.randint(0, 3), "5550001", live, 1)
        t += interval
