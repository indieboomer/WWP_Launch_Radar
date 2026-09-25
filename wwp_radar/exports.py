"""Exports generated from stored data only (never re-fetched from Steam or Twitch).

Date-filter semantics (half-open [from, to), UTC):
  ccu_raw              -> ccu_observations.observed_at
  ccu_aggregates       -> bucket_start
  reviews (csv/json)   -> reviews.timestamp_created (Steam creation time of the review)
  review_versions      -> review_versions.observed_at (when the monitor captured that version)
  review_summaries     -> review_summary_snapshots.observed_at
  review_aggregates    -> bucket_start
  annotations          -> annotations.event_at
  twitch_snapshots     -> twitch_snapshots.observed_at
  twitch_streams       -> observed lifetime overlaps the range (last_seen_at >= from AND first_seen_at < to)
  twitch_stream_observations -> observed_at
  twitch_aggregates    -> bucket_start
The ZIP applies the same rules per dataset. The SQLite backup is always the complete database.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import __version__, store
from .collector import SUMMARY_POPULATIONS
from .db import SCHEMA_VERSION, now

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")

DATASETS = {
    "ccu_raw": {"format": "csv", "time_field": "observed_at",
                "desc": "Surowe obserwacje CCU (tylko udane odczyty). Nieudane zapytania nie są zerami - patrz collection_runs."},
    "ccu_aggregates": {"format": "csv", "time_field": "bucket_start",
                       "desc": "Agregaty CCU 5m/1h/1d: min, max, średnia, liczba próbek, pokrycie."},
    "reviews": {"format": "csv|json", "time_field": "timestamp_created",
                "desc": "Aktualny stan recenzji (wszystkie języki i typy pozyskania; zebrane przez monitor)."},
    "review_versions": {"format": "json", "time_field": "observed_at",
                        "desc": "Niezmienna historia wersji recenzji, z oryginalnym JSON ze Steam."},
    "review_summaries": {"format": "csv", "time_field": "observed_at",
                         "desc": "Migawki query_summary ze Steam, z dokładnymi parametrami zapytania (populacją)."},
    "review_aggregates": {"format": "csv", "time_field": "bucket_start",
                          "desc": "Nowe recenzje pozytywne/negatywne wg czasu utworzenia oraz zmiany rekomendacji wg czasu edycji."},
    "annotations": {"format": "csv", "time_field": "event_at", "desc": "Ręczne adnotacje zdarzeń."},
    "twitch_snapshots": {"format": "csv", "time_field": "observed_at",
                         "desc": "Migawki kategorii Twitch: łączna liczba widzów i kanałów na żywo. Tylko kompletne, "
                                 "udane odczyty - nieudane nie są zerami (patrz collection_runs, source=twitch)."},
    "twitch_streams": {"format": "csv", "time_field": "first_seen_at..last_seen_at",
                       "desc": "Transmisje Twitch widziane w kategorii: kanał, tytuł, język, szczyt i średnia widzów. "
                               "Filtr: okres widoczności transmisji nakłada się na zakres."},
    "twitch_stream_observations": {"format": "csv", "time_field": "observed_at",
                                   "desc": "Liczba widzów każdej transmisji w każdej migawce."},
    "twitch_aggregates": {"format": "csv", "time_field": "bucket_start",
                          "desc": "Agregaty Twitch 5m/1h/1d: widzowie (min/maks./średnia), kanały, unikalne transmisje "
                                  "i kanały, szacowane godziny oglądania, pokrycie."},
}


def iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(ts: int | None, tz: ZoneInfo) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz).isoformat()


def safe_cell(v: Any) -> Any:
    """Neutralise spreadsheet formula injection in text cells (CSV only)."""
    if isinstance(v, str) and v.startswith(FORMULA_PREFIXES):
        return "'" + v
    return v


def write_csv(columns: list[str], rows: Iterable[Iterable[Any]], delimiter: str = ",") -> bytes:
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM so Excel on Windows detects the encoding
    w = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(columns)
    for row in rows:
        w.writerow([safe_cell(v) for v in row])
    return buf.getvalue().encode("utf-8")


def _range(q: str, field: str, start: int | None, end: int | None, args: list) -> str:
    if start is not None:
        q += f" AND {field}>=?"
        args.append(start)
    if end is not None:
        q += f" AND {field}<?"
        args.append(end)
    return q


# -- dataset queries -------------------------------------------------------------------------

def ccu_raw(conn, game_id, start, end, tz):
    args: list = [game_id]
    q = _range("SELECT observed_at, player_count FROM ccu_observations WHERE game_id=?", "observed_at", start, end, args)
    cols = ["observed_at_utc", "observed_at_local", "player_count"]
    rows = [(iso(r[0]), local_iso(r[0], tz), r[1]) for r in conn.execute(q + " ORDER BY observed_at", args)]
    return cols, rows


def ccu_aggregates(conn, game_id, start, end, tz, bucket: str | None = None):
    args: list = [game_id]
    q = "SELECT * FROM ccu_aggregates WHERE game_id=?"
    if bucket:
        q += " AND bucket=?"
        args.append(bucket)
    q = _range(q, "bucket_start", start, end, args)
    cols = ["bucket", "bucket_start_utc", "bucket_end_utc", "bucket_start_local", "samples", "expected_samples",
            "coverage", "min_players", "max_players", "mean_players"]
    rows = [(r["bucket"], iso(r["bucket_start"]), iso(r["bucket_end"]), local_iso(r["bucket_start"], tz), r["samples"],
             r["expected_samples"], r["coverage"], r["min_players"], r["max_players"], r["mean_players"])
            for r in conn.execute(q + " ORDER BY bucket, bucket_start", args)]
    return cols, rows


REVIEW_COLUMNS = [
    "recommendation_id", "steam_url", "voted_up", "language", "timestamp_created_utc", "timestamp_updated_utc",
    "first_seen_at_utc", "last_seen_at_utc", "playtime_at_review_min", "playtime_forever_min", "steam_purchase",
    "received_for_free", "refunded", "written_during_early_access", "primarily_steam_deck", "votes_up", "votes_funny",
    "weighted_vote_score", "comment_count", "version_count", "edited_after_first_seen", "developer_response",
    "timestamp_dev_responded_utc", "author_steamid", "review_text",
]


def review_url(app_id: int, r) -> str:
    if r["author_steamid"]:
        return f"https://steamcommunity.com/profiles/{r['author_steamid']}/recommended/{app_id}/"
    return f"https://store.steampowered.com/app/{app_id}/#app_reviews_hash"


def reviews(conn, game_id, start, end, app_id):
    args: list = [game_id]
    q = _range("SELECT * FROM reviews WHERE game_id=?", "timestamp_created", start, end, args)
    out = []
    for r in conn.execute(q + " ORDER BY timestamp_created", args):
        out.append({
            "recommendation_id": r["recommendation_id"],
            "steam_url": review_url(app_id, r),
            "voted_up": bool(r["voted_up"]),
            "language": r["language"],
            "timestamp_created_utc": iso(r["timestamp_created"]),
            "timestamp_updated_utc": iso(r["timestamp_updated"]),
            "first_seen_at_utc": iso(r["first_seen_at"]),
            "last_seen_at_utc": iso(r["last_seen_at"]),
            "playtime_at_review_min": r["playtime_at_review"],
            "playtime_forever_min": r["playtime_forever"],
            "steam_purchase": _bool(r["steam_purchase"]),
            "received_for_free": _bool(r["received_for_free"]),
            "refunded": _bool(r["refunded"]),
            "written_during_early_access": _bool(r["written_during_early_access"]),
            "primarily_steam_deck": _bool(r["primarily_steam_deck"]),
            "votes_up": r["votes_up"],
            "votes_funny": r["votes_funny"],
            "weighted_vote_score": r["weighted_vote_score"],
            "comment_count": r["comment_count"],
            "version_count": r["version_count"],
            "edited_after_first_seen": r["version_count"] > 1,
            "developer_response": r["developer_response"],
            "timestamp_dev_responded_utc": iso(r["timestamp_dev_responded"]),
            "author_steamid": r["author_steamid"],
            "review_text": r["review_text"],
        })
    return out


def _bool(v):
    return None if v is None else bool(v)


def review_versions(conn, game_id, start, end):
    args: list = [game_id]
    q = _range("SELECT * FROM review_versions WHERE game_id=?", "observed_at", start, end, args)
    out = []
    for r in conn.execute(q + " ORDER BY recommendation_id, version_no", args):
        out.append({
            "recommendation_id": r["recommendation_id"],
            "version_no": r["version_no"],
            "observed_at_utc": iso(r["observed_at"]),
            "timestamp_created_utc": iso(r["timestamp_created"]),
            "timestamp_updated_utc": iso(r["timestamp_updated"]),
            "voted_up": bool(r["voted_up"]),
            "sentiment_changed_from_previous": bool(r["sentiment_changed"]),
            "language": r["language"],
            "playtime_at_review_min": r["playtime_at_review"],
            "steam_purchase": _bool(r["steam_purchase"]),
            "received_for_free": _bool(r["received_for_free"]),
            "written_during_early_access": _bool(r["written_during_early_access"]),
            "developer_response": r["developer_response"],
            "content_hash": r["content_hash"],
            "review_text": r["review_text"],
            "steam_raw": json.loads(r["raw_json"]),
        })
    return out


def review_summaries(conn, game_id, start, end, tz):
    args: list = [game_id]
    q = _range("SELECT * FROM review_summary_snapshots WHERE game_id=?", "observed_at", start, end, args)
    cols = ["observed_at_utc", "observed_at_local", "population", "population_label", "query_params", "total_positive",
            "total_negative", "total_reviews", "positive_pct", "review_score", "review_score_desc"]
    rows = []
    for r in conn.execute(q + " ORDER BY observed_at, population", args):
        voted = r["total_positive"] + r["total_negative"]
        pct = round(100 * r["total_positive"] / voted, 2) if voted else None
        label = SUMMARY_POPULATIONS.get(r["population"], {}).get("label", r["population"])
        rows.append((iso(r["observed_at"]), local_iso(r["observed_at"], tz), r["population"], label, r["query_params"],
                     r["total_positive"], r["total_negative"], r["total_reviews"], pct, r["review_score"],
                     r["review_score_desc"]))
    return cols, rows


def review_aggregates(conn, game_id, start, end, tz):
    args: list = [game_id]
    q = _range("SELECT * FROM review_aggregates WHERE game_id=?", "bucket_start", start, end, args)
    cols = ["bucket", "bucket_start_utc", "bucket_end_utc", "bucket_start_local", "new_positive", "new_negative",
            "new_positive_steam_purchase", "new_negative_steam_purchase", "changed_to_positive", "changed_to_negative"]
    rows = [(r["bucket"], iso(r["bucket_start"]), iso(r["bucket_end"]), local_iso(r["bucket_start"], tz),
             r["new_positive"], r["new_negative"], r["new_positive_steam"], r["new_negative_steam"],
             r["changed_to_positive"], r["changed_to_negative"])
            for r in conn.execute(q + " ORDER BY bucket, bucket_start", args)]
    return cols, rows


def annotations(conn, game_id, start, end, tz):
    cols = ["id", "event_at_utc", "event_at_local", "kind", "title", "description", "created_at_utc", "updated_at_utc"]
    rows = [(a["id"], iso(a["event_at"]), local_iso(a["event_at"], tz), a["kind"], a["title"], a["description"],
             iso(a["created_at"]), iso(a["updated_at"])) for a in store.list_annotations(conn, game_id, start, end)]
    return cols, rows


def twitch_snapshots(conn, game_id, start, end, tz):
    args: list = [game_id]
    q = _range("SELECT * FROM twitch_snapshots WHERE game_id=?", "observed_at", start, end, args)
    cols = ["observed_at_utc", "observed_at_local", "category_id", "live_channels", "total_viewers", "pages"]
    rows = [(iso(r["observed_at"]), local_iso(r["observed_at"], tz), r["category_id"], r["live_channels"],
             r["total_viewers"], r["pages"]) for r in conn.execute(q + " ORDER BY observed_at", args)]
    return cols, rows


def twitch_streams(conn, game_id, start, end):
    args: list = [game_id]
    q = "SELECT * FROM twitch_streams WHERE game_id=?"
    # A stream belongs to the range when its observed lifetime overlaps [start, end).
    if start is not None:
        q += " AND last_seen_at>=?"
        args.append(start)
    if end is not None:
        q += " AND first_seen_at<?"
        args.append(end)
    cols = ["stream_id", "twitch_url", "user_id", "user_login", "user_name", "category_id", "title", "language",
            "is_mature", "tags", "started_at_utc", "first_seen_at_utc", "last_seen_at_utc", "samples", "peak_viewers",
            "peak_at_utc", "mean_viewers"]
    rows = [(r["stream_id"], f"https://www.twitch.tv/{r['user_login']}", r["user_id"], r["user_login"], r["user_name"],
             r["category_id"], r["title"], r["language"], _bool(r["is_mature"]), r["tags"], iso(r["started_at"]),
             iso(r["first_seen_at"]), iso(r["last_seen_at"]), r["samples"], r["peak_viewers"], iso(r["peak_at"]),
             round(r["viewer_sum"] / r["samples"], 2) if r["samples"] else None)
            for r in conn.execute(q + " ORDER BY first_seen_at, stream_id", args)]
    return cols, rows


def twitch_stream_observations(conn, game_id, start, end):
    args: list = [game_id]
    q = _range("SELECT o.*, s.user_login FROM twitch_stream_observations o JOIN twitch_streams s "
               "ON s.game_id=o.game_id AND s.stream_id=o.stream_id WHERE o.game_id=?", "o.observed_at", start, end, args)
    cols = ["observed_at_utc", "snapshot_id", "stream_id", "user_login", "viewer_count"]
    rows = [(iso(r["observed_at"]), r["snapshot_id"], r["stream_id"], r["user_login"], r["viewer_count"])
            for r in conn.execute(q + " ORDER BY o.observed_at, o.viewer_count DESC", args)]
    return cols, rows


def twitch_aggregates(conn, game_id, start, end, tz, bucket: str | None = None):
    args: list = [game_id]
    q = "SELECT * FROM twitch_aggregates WHERE game_id=?"
    if bucket:
        q += " AND bucket=?"
        args.append(bucket)
    q = _range(q, "bucket_start", start, end, args)
    cols = ["bucket", "bucket_start_utc", "bucket_end_utc", "bucket_start_local", "samples", "expected_samples",
            "coverage", "min_viewers", "max_viewers", "mean_viewers", "max_channels", "mean_channels", "unique_streams",
            "unique_channels", "viewer_hours_estimate"]
    rows = [(r["bucket"], iso(r["bucket_start"]), iso(r["bucket_end"]), local_iso(r["bucket_start"], tz), r["samples"],
             r["expected_samples"], r["coverage"], r["min_viewers"], r["max_viewers"], r["mean_viewers"],
             r["max_channels"], r["mean_channels"], r["unique_streams"], r["unique_channels"], r["viewer_hours"])
            for r in conn.execute(q + " ORDER BY bucket, bucket_start", args)]
    return cols, rows


def reviews_csv(conn, game_id, start, end, app_id, delimiter=","):
    data = reviews(conn, game_id, start, end, app_id)
    return write_csv(REVIEW_COLUMNS, ([d[c] for c in REVIEW_COLUMNS] for d in data), delimiter)


# -- metadata -------------------------------------------------------------------------------------

def ccu_gaps(conn, game_id, start, end, interval) -> list[dict]:
    """Intervals longer than 2.5 poll intervals without a successful CCU observation."""
    return _gaps(conn, game_id, start, end, interval, "ccu_observations", "monitoring_started_at")


def twitch_gaps(conn, game_id, start, end, interval) -> list[dict]:
    """Intervals longer than 2.5 poll intervals without a successful Twitch snapshot."""
    return _gaps(conn, game_id, start, end, interval, "twitch_snapshots", "twitch_monitoring_started_at")


def _gaps(conn, game_id, start, end, interval, table: str, started_column: str) -> list[dict]:
    args: list = [game_id]
    q = _range(f"SELECT observed_at FROM {table} WHERE game_id=?", "observed_at", start, end, args)
    times = [r[0] for r in conn.execute(q + " ORDER BY observed_at", args)]
    game = store.get_game(conn, game_id)
    if game[started_column] is None:
        return []
    lo = max(start or 0, game[started_column])
    hi = min(end, now()) if end else now()
    if hi <= lo:
        return []
    threshold = 2.5 * interval
    points = [lo] + times + [hi]
    gaps = []
    for a, b in zip(points, points[1:]):
        if b - a > threshold:
            gaps.append({"from_utc": iso(a), "to_utc": iso(b), "seconds": b - a})
    return gaps


def metadata(conn, game_id, start, end, settings) -> dict:
    game = store.get_game(conn, game_id)
    imp = store.get_checkpoint(conn, game_id, "import") or {"status": "not_started"}
    gaps = ccu_gaps(conn, game_id, start, end, settings.ccu_interval)
    first_obs = conn.execute("SELECT MIN(observed_at), MAX(observed_at), COUNT(*) FROM ccu_observations WHERE game_id=?",
                             (game_id,)).fetchone()
    twitch_gap_list = twitch_gaps(conn, game_id, start, end, settings.twitch_interval)
    twitch_count = conn.execute("SELECT COUNT(*) FROM twitch_snapshots WHERE game_id=?", (game_id,)).fetchone()[0]
    return {
        "application": "WWP Launch Radar",
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "demo_data": settings.demo,
        "game": {"app_id": game["app_id"], "name": game["name"], "launch_at_utc": iso(game["launch_at"])},
        "exported_at_utc": iso(now()),
        "selected_range": {"from_utc": iso(start), "to_utc": iso(end), "semantics": "half-open [from, to), UTC"},
        "timezone_conventions": {
            "storage": "UTC (Unix epoch seconds in the database)",
            "exports": "*_utc columns are ISO-8601 UTC (suffix Z); *_local columns use the display timezone",
            "display_timezone": settings.display_tz,
            "daily_buckets": f"calendar days in {settings.display_tz}",
        },
        "datasets": {k: {"file_format": v["format"], "date_filter_field": v["time_field"], "definition": v["desc"]}
                     for k, v in DATASETS.items()},
        "review_record_query": {"language": "all", "purchase_type": "all", "review_type": "all",
                                "filter_offtopic_activity": 0, "filters": ["recent", "updated"]},
        "summary_populations": {k: {"label": v["label"], "query_params": v["params"]} for k, v in SUMMARY_POPULATIONS.items()},
        "collection": {
            "monitoring_started_at_utc": iso(game["monitoring_started_at"]),
            "ccu_poll_interval_seconds": settings.ccu_interval,
            "review_poll_interval_seconds": settings.review_interval,
            "ccu_first_observation_utc": iso(first_obs[0]),
            "ccu_last_observation_utc": iso(first_obs[1]),
            "ccu_observation_count_total": first_obs[2],
            "ccu_gaps_in_range": gaps[:2000],
            "ccu_gap_count_in_range": len(gaps),
            "ccu_gap_definition": f"no successful observation for more than {2.5 * settings.ccu_interval:.0f} s",
            "review_import": {**imp, "started_at": iso(imp.get("started_at")), "completed_at": iso(imp.get("completed_at"))},
        },
        "twitch": {
            "configured": settings.twitch_available,
            "category_query": game["twitch_category"] or settings.twitch_category or game["name"],
            "category_id": game["twitch_category_id"],
            "category_name": game["twitch_category_name"],
            "monitoring_started_at_utc": iso(game["twitch_monitoring_started_at"]),
            "poll_interval_seconds": settings.twitch_interval,
            "snapshot_count_total": twitch_count,
            "gaps_in_range": twitch_gap_list[:2000],
            "gap_count_in_range": len(twitch_gap_list),
            "gap_definition": f"no successful snapshot for more than {2.5 * settings.twitch_interval:.0f} s",
        },
        "metric_definitions": METRIC_DEFINITIONS,
        "limitations": LIMITATIONS,
    }


METRIC_DEFINITIONS = {
    "ccu": "Liczba graczy online zwrócona przez ISteamUserStats/GetNumberOfCurrentPlayers w chwili odczytu.",
    "highest_observed_ccu": "Najwyższy CCU zaobserwowany od rozpoczęcia monitoringu - NIE jest to rekord all-time Steam.",
    "ccu_coverage": "samples / expected_samples w kubełku; expected = monitorowane sekundy w kubełku / interwał odpytywania.",
    "new_reviews": "Recenzje zliczane wg czasu utworzenia na Steam (timestamp_created), klasyfikowane wg bieżącej rekomendacji.",
    "sentiment_changes": "Zmiana rekomendacji zaobserwowana przez monitor między kolejnymi wersjami; czas = timestamp_updated.",
    "positive_pct": "total_positive / (total_positive + total_negative) z migawki Steam dla danej populacji.",
    "first_seen_at": "Chwila, w której monitor po raz pierwszy zobaczył recenzję (czas odkrycia, nie publikacji).",
    "twitch_total_viewers": "Suma viewer_count wszystkich transmisji na żywo w kategorii gry w chwili migawki (Twitch Helix).",
    "twitch_live_channels": "Liczba różnych kanałów nadających na żywo w kategorii w chwili migawki.",
    "twitch_viewer_hours": "Szacunek: suma widzów z migawek × interwał odpytywania / 3600. Przy pokryciu < 1 zaniżony.",
    "twitch_peak_viewers": "Najwyższa zaobserwowana wartość od rozpoczęcia monitoringu Twitch - nie oficjalna statystyka Twitch.",
}

LIMITATIONS = [
    "CCU sprzed rozpoczęcia monitoringu nie jest dostępny z tego API.",
    "Nieudane odczyty CCU nie są zapisywane jako zero; tworzą luki widoczne w danych i w pokryciu agregatów.",
    "Z CCU nie wynikają unikalni gracze, retencja ani liczba sprzedanych kopii.",
    "Liczby recenzji to dane zaimportowane/zaobserwowane; przy niekompletnym imporcie mogą być niepełne.",
    "Recenzja nieobecna w wyniku przyrostowym nie jest traktowana jako usunięta.",
    "Zmiany rekomendacji, które nastąpiły przed pierwszą obserwacją recenzji, nie są znane.",
    "Migawki podsumowania Steam z różnych populacji (parametrów zapytania) nie są mieszane.",
    "Twitch: widoczne są tylko transmisje w kategorii gry; transmisje w innej kategorii (np. Just Chatting) nie są liczone.",
    "Twitch: krótkie transmisje między migawkami mogą zostać pominięte; godziny oglądania to szacunek z próbek.",
    "Twitch: viewer_count pochodzi z Twitch i może być opóźniony lub zaokrąglony; nieudane migawki tworzą luki.",
]


# -- bundles ----------------------------------------------------------------------------------

def build_zip(conn: sqlite3.Connection, game_id: int, start, end, settings, dest: Path, delimiter: str = ",") -> Path:
    tz = ZoneInfo(settings.display_tz)
    game = store.get_game(conn, game_id)
    conn.execute("BEGIN")  # one read snapshot for all datasets (WAL)
    try:
        meta = metadata(conn, game_id, start, end, settings)
        files: dict[str, bytes] = {
            "ccu_raw.csv": write_csv(*ccu_raw(conn, game_id, start, end, tz), delimiter),
            "ccu_aggregates.csv": write_csv(*ccu_aggregates(conn, game_id, start, end, tz), delimiter),
            "reviews.csv": reviews_csv(conn, game_id, start, end, game["app_id"], delimiter),
            "reviews.json": _json(reviews(conn, game_id, start, end, game["app_id"])),
            "review_versions.json": _json(review_versions(conn, game_id, start, end)),
            "review_summaries.csv": write_csv(*review_summaries(conn, game_id, start, end, tz), delimiter),
            "review_aggregates.csv": write_csv(*review_aggregates(conn, game_id, start, end, tz), delimiter),
            "annotations.csv": write_csv(*annotations(conn, game_id, start, end, tz), delimiter),
            "twitch_snapshots.csv": write_csv(*twitch_snapshots(conn, game_id, start, end, tz), delimiter),
            "twitch_streams.csv": write_csv(*twitch_streams(conn, game_id, start, end), delimiter),
            "twitch_stream_observations.csv": write_csv(*twitch_stream_observations(conn, game_id, start, end), delimiter),
            "twitch_aggregates.csv": write_csv(*twitch_aggregates(conn, game_id, start, end, tz), delimiter),
        }
    finally:
        conn.execute("COMMIT")
    files["metadata.json"] = _json(meta)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return dest


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
