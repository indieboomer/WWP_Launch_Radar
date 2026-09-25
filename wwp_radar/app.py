"""FastAPI application: dashboard, JSON API, exports; owns the collector lifecycle."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import __version__, aggregates, ai, auth, exports, store
from .collector import SUMMARY_POPULATIONS, Collector
from .config import Settings, load_settings, parse_launch_at, validate_exposure
from .db import SCHEMA_VERSION, Database, now, transaction

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def bootstrap(db: Database, settings: Settings) -> int:
    """Ensure the configured default game exists and an active game is selected."""
    conn = db.conn()
    with transaction(conn):
        gid = store.active_game_id(conn)
        if gid is None or store.get_game(conn, gid) is None:
            gid = store.ensure_game(conn, settings.app_id, settings.game_name)
            store.set_setting(conn, "active_game_id", str(gid))
        game = store.get_game(conn, gid)
        if settings.launch_at and game["launch_at"] is None and game["app_id"] == settings.app_id:
            conn.execute("UPDATE games SET launch_at=? WHERE id=?", (parse_launch_at(settings.launch_at, settings.display_tz), gid))
    return gid


def create_app(settings: Settings | None = None, start_collector: bool | None = None) -> FastAPI:
    settings = settings or load_settings()
    validate_exposure(settings)
    db = Database(settings.db_path)
    bootstrap(db, settings)
    secret = auth.load_secret(settings.session_secret, settings.data_dir) if settings.auth_enabled else b""
    collector = Collector(settings, db)
    run_collector = settings.collector_enabled and not settings.demo if start_collector is None else start_collector

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_collector:
            await asyncio.to_thread(collector.start)
        else:
            gid = store.active_game_id(db.conn())
            await asyncio.to_thread(aggregates.rebuild_all, db.conn(), gid, settings.display_tz, settings.ccu_interval, settings.twitch_interval)
        yield
        await asyncio.to_thread(collector.stop)

    app = FastAPI(title="WWP Launch Radar", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.collector = collector
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- auth ----------------------------------------------------------------------------
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if settings.auth_enabled and request.url.path not in auth.PUBLIC_PATHS:
            if not auth.check_token(secret, settings.dashboard_password, request.cookies.get(auth.COOKIE_NAME)):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "authentication required"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: int = 0):
        html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
        return html.replace("{{ERROR}}", "Nieprawidłowe hasło." if error else "")

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        if not settings.auth_enabled:
            return RedirectResponse("/", status_code=303)
        if not auth.check_password(settings.dashboard_password, str(form.get("password", ""))):
            await asyncio.sleep(1.0)  # slow down guessing
            return RedirectResponse("/login?error=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.COOKIE_NAME, auth.make_token(secret, settings.dashboard_password), max_age=auth.SESSION_TTL,
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login" if settings.auth_enabled else "/", status_code=303)
        resp.delete_cookie(auth.COOKIE_NAME)
        return resp

    # -- pages & health ----------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.svg")
    def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/healthz")
    def healthz():
        try:
            db.conn().execute("SELECT 1").fetchone()
        except Exception:
            return JSONResponse({"status": "error", "db": False}, status_code=503)
        collecting = collector.running
        stale = False
        if collecting:
            hb = collector.heartbeat.get("ccu")
            stale = hb is not None and now() - hb > max(300, 5 * settings.ccu_interval)
        ok = not stale
        return JSONResponse({"status": "ok" if ok else "degraded", "collector": collecting, "stale": stale},
                            status_code=200 if ok else 503)

    # -- helpers ---------------------------------------------------------------------------------
    def conn():
        return db.conn()

    def game_or_404(game_id: Optional[int]) -> dict:
        c = conn()
        gid = game_id if game_id is not None else store.active_game_id(c)
        g = store.get_game(c, gid) if gid is not None else None
        if g is None:
            raise HTTPException(404, "unknown game")
        return dict(g)

    def import_status(game_id: int) -> dict:
        imp = store.get_checkpoint(conn(), game_id, "import") or {"status": "not_started"}
        stored = conn().execute("SELECT COUNT(*) FROM reviews WHERE game_id=?", (game_id,)).fetchone()[0]
        return {**imp, "stored_reviews": stored}

    # -- status & overview --------------------------------------------------------------------
    @app.get("/api/status")
    def status(game_id: Optional[int] = None):
        g = game_or_404(game_id)
        c = conn()
        health = {h["source"]: h for h in store.source_health(c, g["id"])}
        first_review = c.execute("SELECT MIN(timestamp_created) FROM reviews WHERE game_id=?", (g["id"],)).fetchone()[0]
        return {
            "app_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "demo": settings.demo,
            "now": now(),
            "display_tz": settings.display_tz,
            "game": g,
            "games": store.list_games(c),
            "active_game_id": store.active_game_id(c),
            "intervals": {"ccu": settings.ccu_interval, "reviews": settings.review_interval},
            "collector": {
                "running": collector.running,
                "enabled": run_collector,
                "lock_error": collector.lock_error,
                "heartbeat": collector.heartbeat,
            },
            "sources": health,
            "import": import_status(g["id"]),
            "data_start": min(x for x in (g["monitoring_started_at"], first_review, now()) if x is not None),
            "ai": {"enabled": settings.ai_enabled, "available": settings.ai_available, "model": settings.ai_model},
            "twitch": {
                "available": settings.twitch_available,
                "interval": settings.twitch_interval,
                "category_query": twitch_category_query(g),
                "category_id": g["twitch_category_id"],
                "category_name": g["twitch_category_name"],
            },
            "auth": settings.auth_enabled,
            "summary_populations": {k: v["label"] for k, v in SUMMARY_POPULATIONS.items()},
        }

    def nearest_sample(c, game_id: int, target: int, tolerance: int):
        row = c.execute(
            "SELECT observed_at, player_count FROM ccu_observations WHERE game_id=? AND observed_at BETWEEN ? AND ? "
            "ORDER BY ABS(observed_at-?) LIMIT 1",
            (game_id, target - tolerance, target + tolerance, target),
        ).fetchone()
        return dict(row) if row else None

    @app.get("/api/overview")
    def overview(game_id: Optional[int] = None, population: str = "all_all"):
        g = game_or_404(game_id)
        if population not in SUMMARY_POPULATIONS:
            raise HTTPException(400, "unknown population")
        c = conn()
        gid = g["id"]
        t = now()
        latest = c.execute("SELECT observed_at, player_count FROM ccu_observations WHERE game_id=? "
                           "ORDER BY observed_at DESC LIMIT 1", (gid,)).fetchone()
        peak = c.execute("SELECT observed_at, player_count FROM ccu_observations WHERE game_id=? "
                         "ORDER BY player_count DESC, observed_at ASC LIMIT 1", (gid,)).fetchone()
        changes = {}
        for minutes, tol in ((15, 180), (60, 300)):
            if latest is None:
                changes[str(minutes)] = None
                continue
            ref = nearest_sample(c, gid, latest["observed_at"] - minutes * 60, tol)
            if ref is None:
                changes[str(minutes)] = None
            else:
                diff = latest["player_count"] - ref["player_count"]
                pct = round(100 * diff / ref["player_count"], 1) if ref["player_count"] else None
                changes[str(minutes)] = {"diff": diff, "pct": pct, "reference_at": ref["observed_at"],
                                         "reference_count": ref["player_count"]}
        snap = c.execute("SELECT * FROM review_summary_snapshots WHERE game_id=? AND population=? "
                         "ORDER BY observed_at DESC LIMIT 1", (gid, population)).fetchone()
        summary = None
        if snap:
            voted = snap["total_positive"] + snap["total_negative"]
            summary = {
                "observed_at": snap["observed_at"], "total_positive": snap["total_positive"],
                "total_negative": snap["total_negative"], "total_reviews": snap["total_reviews"],
                "positive_pct": round(100 * snap["total_positive"] / voted, 1) if voted else None,
                "review_score_desc": snap["review_score_desc"], "population": population,
                "population_label": SUMMARY_POPULATIONS[population]["label"],
            }
        stored = c.execute(
            "SELECT SUM(voted_up=1) AS pos, SUM(voted_up=0) AS neg, "
            "SUM(voted_up=1 AND steam_purchase=1) AS pos_steam, SUM(voted_up=0 AND steam_purchase=1) AS neg_steam, "
            "SUM(timestamp_created>=?) AS last_hour, SUM(timestamp_created>=? AND voted_up=1) AS last_hour_pos "
            "FROM reviews WHERE game_id=?",
            (t - 3600, t - 3600, gid),
        ).fetchone()
        last_ccu_run = c.execute("SELECT status, finished_at, error FROM collection_runs WHERE game_id=? AND source='ccu' "
                                 "ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
        return {
            "now": t,
            "ccu": {
                "current": dict(latest) if latest else None,
                "stale": latest is None or t - latest["observed_at"] > 3 * settings.ccu_interval,
                "last_run": dict(last_ccu_run) if last_ccu_run else None,
                "peak": dict(peak) if peak else None,
                "change": changes,
            },
            "summary": summary,
            "stored_reviews": {k: (stored[k] or 0) for k in stored.keys()},
            "import": import_status(gid),
        }

    # -- series -------------------------------------------------------------------------------
    def resolve(start: Optional[int], end: Optional[int], g: dict) -> tuple[int, int]:
        t = now()
        end = min(end or t, t + 60)
        if start is None:
            c = conn()
            candidates = [g["monitoring_started_at"],
                          c.execute("SELECT MIN(timestamp_created) FROM reviews WHERE game_id=?", (g["id"],)).fetchone()[0]]
            candidates = [x for x in candidates if x is not None]
            start = min(candidates) if candidates else end - 3600
        if start >= end:
            raise HTTPException(400, "empty range")
        return start, end

    @app.get("/api/ccu")
    def ccu_series(game_id: Optional[int] = None, start: Optional[int] = Query(None, alias="from"),
                   end: Optional[int] = Query(None, alias="to")):
        g = game_or_404(game_id)
        start, end = resolve(start, end, g)
        span = end - start
        c = conn()
        if span <= 12 * 3600:
            rows = c.execute("SELECT observed_at, player_count FROM ccu_observations WHERE game_id=? AND observed_at>=? "
                             "AND observed_at<? ORDER BY observed_at", (g["id"], start, end)).fetchall()
            gap = 2.5 * settings.ccu_interval
            ts, val = [], []
            prev = None
            for r in rows:
                if prev is not None and r[0] - prev > gap:
                    ts.append(prev + settings.ccu_interval)
                    val.append(None)  # explicit gap marker, not an observation
                ts.append(r[0])
                val.append(r[1])
                prev = r[0]
            return {"resolution": "raw", "t": ts, "value": val, "max": None, "min": None, "coverage": None}
        bucket = "5m" if span <= 7 * 86400 else "1h" if span <= 90 * 86400 else "1d"
        rows = c.execute("SELECT * FROM ccu_aggregates WHERE game_id=? AND bucket=? AND bucket_start>=? AND bucket_start<? "
                         "ORDER BY bucket_start", (g["id"], bucket, aggregates.bucket_bounds(start, bucket, ZoneInfo(settings.display_tz))[0], end)).fetchall()
        return {
            "resolution": bucket,
            "t": [r["bucket_start"] for r in rows],
            "value": [r["mean_players"] for r in rows],
            "max": [r["max_players"] for r in rows],
            "min": [r["min_players"] for r in rows],
            "coverage": [r["coverage"] for r in rows],
        }

    @app.get("/api/reviews/series")
    def review_series(game_id: Optional[int] = None, start: Optional[int] = Query(None, alias="from"),
                      end: Optional[int] = Query(None, alias="to"), steam_only: bool = False):
        g = game_or_404(game_id)
        start, end = resolve(start, end, g)
        bucket = "1h" if end - start <= 4 * 86400 else "1d"
        tz = ZoneInfo(settings.display_tz)
        rows = {r["bucket_start"]: r for r in conn().execute(
            "SELECT * FROM review_aggregates WHERE game_id=? AND bucket=? AND bucket_start>=? AND bucket_start<?",
            (g["id"], bucket, aggregates.bucket_bounds(start, bucket, tz)[0], end))}
        out = {"resolution": bucket, "t": [], "positive": [], "negative": [], "to_positive": [], "to_negative": []}
        for bs, _ in aggregates.iter_buckets(start, end, bucket, tz):
            r = rows.get(bs)
            out["t"].append(bs)
            if r is None:
                for k in ("positive", "negative", "to_positive", "to_negative"):
                    out[k].append(0)
                continue
            out["positive"].append(r["new_positive_steam"] if steam_only else r["new_positive"])
            out["negative"].append(r["new_negative_steam"] if steam_only else r["new_negative"])
            out["to_positive"].append(r["changed_to_positive"])
            out["to_negative"].append(r["changed_to_negative"])
        return out

    @app.get("/api/summary/series")
    def summary_series(game_id: Optional[int] = None, start: Optional[int] = Query(None, alias="from"),
                       end: Optional[int] = Query(None, alias="to"), population: str = "all_all"):
        g = game_or_404(game_id)
        start, end = resolve(start, end, g)
        c = conn()
        if end - start <= 3 * 86400:
            rows = c.execute("SELECT observed_at AS t, total_positive, total_negative, total_reviews FROM review_summary_snapshots "
                             "WHERE game_id=? AND population=? AND observed_at>=? AND observed_at<? ORDER BY observed_at",
                             (g["id"], population, start, end)).fetchall()
            res = "raw"
        else:
            rows = c.execute("SELECT last_observed_at AS t, total_positive, total_negative, total_reviews FROM summary_aggregates "
                             "WHERE game_id=? AND bucket='1h' AND population=? AND bucket_start>=? AND bucket_start<? "
                             "ORDER BY bucket_start", (g["id"], population, start - 3600, end)).fetchall()
            res = "1h"
        pct = []
        for r in rows:
            voted = r["total_positive"] + r["total_negative"]
            pct.append(round(100 * r["total_positive"] / voted, 2) if voted else None)
        return {"resolution": res, "population": population, "label": SUMMARY_POPULATIONS.get(population, {}).get("label"),
                "t": [r["t"] for r in rows], "pct": pct, "total": [r["total_reviews"] for r in rows]}

    # -- Twitch ---------------------------------------------------------------------------------
    def twitch_category_query(g: dict) -> str:
        return (g["twitch_category"] or settings.twitch_category or g["name"]).strip()

    def twitch_url(login: str) -> str:
        return f"https://www.twitch.tv/{login}"

    @app.get("/api/twitch/overview")
    def twitch_overview(game_id: Optional[int] = None, start: Optional[int] = Query(None, alias="from"),
                        end: Optional[int] = Query(None, alias="to")):
        g = game_or_404(game_id)
        gid = g["id"]
        c = conn()
        t = now()
        start, end = resolve(start, end, g)
        latest = c.execute("SELECT * FROM twitch_snapshots WHERE game_id=? ORDER BY observed_at DESC LIMIT 1",
                           (gid,)).fetchone()
        stale = latest is None or t - latest["observed_at"] > 3 * settings.twitch_interval
        peak = c.execute("SELECT observed_at, total_viewers FROM twitch_snapshots WHERE game_id=? "
                         "ORDER BY total_viewers DESC, observed_at ASC LIMIT 1", (gid,)).fetchone()
        peak_ch = c.execute("SELECT observed_at, live_channels FROM twitch_snapshots WHERE game_id=? "
                            "ORDER BY live_channels DESC, observed_at ASC LIMIT 1", (gid,)).fetchone()
        last_run = c.execute("SELECT status, finished_at, error FROM collection_runs WHERE game_id=? AND source='twitch' "
                             "ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
        live = []
        if latest is not None and not stale:
            for r in c.execute(
                    "SELECT s.*, o.viewer_count FROM twitch_stream_observations o JOIN twitch_streams s "
                    "ON s.game_id=o.game_id AND s.stream_id=o.stream_id WHERE o.snapshot_id=? "
                    "ORDER BY o.viewer_count DESC, s.user_login", (latest["id"],)):
                live.append({"stream_id": r["stream_id"], "user_login": r["user_login"], "user_name": r["user_name"],
                             "title": r["title"], "language": r["language"], "viewers": r["viewer_count"],
                             "started_at": r["started_at"], "peak_viewers": r["peak_viewers"],
                             "url": twitch_url(r["user_login"])})
        rng = c.execute(
            "SELECT COUNT(*) AS samples, SUM(total_viewers) AS viewer_sum, MAX(total_viewers) AS peak_viewers, "
            "MAX(live_channels) AS peak_channels, AVG(total_viewers) AS mean_viewers FROM twitch_snapshots "
            "WHERE game_id=? AND observed_at>=? AND observed_at<?", (gid, start, end)).fetchone()
        uniq = c.execute(
            "SELECT COUNT(DISTINCT o.stream_id) AS streams, COUNT(DISTINCT s.user_id) AS channels "
            "FROM twitch_stream_observations o JOIN twitch_streams s ON s.game_id=o.game_id AND s.stream_id=o.stream_id "
            "WHERE o.game_id=? AND o.observed_at>=? AND o.observed_at<?", (gid, start, end)).fetchone()
        top = []
        for r in c.execute(
                "SELECT o.stream_id, MAX(o.viewer_count) AS peak, AVG(o.viewer_count) AS mean, COUNT(*) AS samples, "
                "MIN(o.observed_at) AS first_at, MAX(o.observed_at) AS last_at, s.user_login, s.user_name, s.title, "
                "s.language, s.started_at FROM twitch_stream_observations o JOIN twitch_streams s "
                "ON s.game_id=o.game_id AND s.stream_id=o.stream_id "
                "WHERE o.game_id=? AND o.observed_at>=? AND o.observed_at<? "
                "GROUP BY o.stream_id ORDER BY peak DESC, samples DESC LIMIT 25", (gid, start, end)):
            top.append({**dict(r), "mean": round(r["mean"], 1),
                        "viewer_hours": round(r["mean"] * r["samples"] * settings.twitch_interval / 3600, 1),
                        "url": twitch_url(r["user_login"])})
        return {
            "now": t,
            "available": settings.twitch_available,
            "category": {"query": twitch_category_query(g), "id": g["twitch_category_id"],
                         "name": g["twitch_category_name"]},
            "current": ({"observed_at": latest["observed_at"], "total_viewers": latest["total_viewers"],
                         "live_channels": latest["live_channels"]} if latest else None),
            "stale": stale,
            "last_run": dict(last_run) if last_run else None,
            "peak_viewers": dict(peak) if peak else None,
            "peak_channels": dict(peak_ch) if peak_ch else None,
            "live": live,
            "range": {
                "from": start, "to": end, "samples": rng["samples"],
                "peak_viewers": rng["peak_viewers"], "peak_channels": rng["peak_channels"],
                "mean_viewers": round(rng["mean_viewers"], 1) if rng["mean_viewers"] is not None else None,
                "viewer_hours": round((rng["viewer_sum"] or 0) * settings.twitch_interval / 3600, 1),
                "unique_streams": uniq["streams"], "unique_channels": uniq["channels"],
            },
            "top_streams": top,
        }

    @app.get("/api/twitch/series")
    def twitch_series(game_id: Optional[int] = None, start: Optional[int] = Query(None, alias="from"),
                      end: Optional[int] = Query(None, alias="to")):
        g = game_or_404(game_id)
        start, end = resolve(start, end, g)
        span = end - start
        c = conn()
        if span <= 12 * 3600:
            rows = c.execute("SELECT observed_at, total_viewers, live_channels FROM twitch_snapshots WHERE game_id=? "
                             "AND observed_at>=? AND observed_at<? ORDER BY observed_at", (g["id"], start, end)).fetchall()
            gap = 2.5 * settings.twitch_interval
            ts, viewers, channels = [], [], []
            prev = None
            for r in rows:
                if prev is not None and r[0] - prev > gap:
                    ts.append(prev + settings.twitch_interval)  # explicit gap marker, not an observation
                    viewers.append(None)
                    channels.append(None)
                ts.append(r[0])
                viewers.append(r[1])
                channels.append(r[2])
                prev = r[0]
            return {"resolution": "raw", "t": ts, "viewers": viewers, "channels": channels, "max": None, "coverage": None}
        bucket = "5m" if span <= 7 * 86400 else "1h" if span <= 90 * 86400 else "1d"
        rows = c.execute("SELECT * FROM twitch_aggregates WHERE game_id=? AND bucket=? AND bucket_start>=? "
                         "AND bucket_start<? ORDER BY bucket_start",
                         (g["id"], bucket, aggregates.bucket_bounds(start, bucket, ZoneInfo(settings.display_tz))[0],
                          end)).fetchall()
        return {
            "resolution": bucket,
            "t": [r["bucket_start"] for r in rows],
            "viewers": [r["mean_viewers"] for r in rows],
            "channels": [r["mean_channels"] for r in rows],
            "max": [r["max_viewers"] for r in rows],
            "coverage": [r["coverage"] for r in rows],
        }

    # -- review feed ---------------------------------------------------------------------------
    @app.get("/api/languages")
    def languages(game_id: Optional[int] = None):
        g = game_or_404(game_id)
        return [dict(r) for r in conn().execute(
            "SELECT language, COUNT(*) AS n FROM reviews WHERE game_id=? GROUP BY language ORDER BY n DESC", (g["id"],))]

    @app.get("/api/reviews")
    def review_feed(game_id: Optional[int] = None, sentiment: str = "all", language: str = "", purchase: str = "all",
                    q: str = "", edited: bool = False, start: Optional[int] = Query(None, alias="from"),
                    end: Optional[int] = Query(None, alias="to"), limit: int = Query(50, le=200), offset: int = 0):
        g = game_or_404(game_id)
        where = ["game_id=?"]
        args: list = [g["id"]]
        if sentiment in ("positive", "negative"):
            where.append("voted_up=?")
            args.append(1 if sentiment == "positive" else 0)
        if language:
            where.append("language=?")
            args.append(language)
        if purchase == "steam":
            where.append("steam_purchase=1")
        elif purchase == "other":
            where.append("(steam_purchase IS NULL OR steam_purchase=0)")
        if q:
            where.append("review_text LIKE ? ESCAPE '\\'")
            args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if edited:
            where.append("version_count>1")
        if start is not None:
            where.append("timestamp_created>=?")
            args.append(start)
        if end is not None:
            where.append("timestamp_created<?")
            args.append(end)
        w = " AND ".join(where)
        c = conn()
        total = c.execute(f"SELECT COUNT(*) FROM reviews WHERE {w}", args).fetchone()[0]
        rows = c.execute(f"SELECT * FROM reviews WHERE {w} ORDER BY timestamp_created DESC LIMIT ? OFFSET ?",
                         args + [limit, offset]).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["steam_url"] = exports.review_url(g["app_id"], r)
            d.pop("content_hash", None)
            items.append(d)
        return {"total": total, "items": items}

    @app.get("/api/reviews/{rid}/versions")
    def review_versions(rid: str, game_id: Optional[int] = None):
        g = game_or_404(game_id)
        rows = conn().execute(
            "SELECT version_no, observed_at, timestamp_created, timestamp_updated, voted_up, review_text, language, "
            "playtime_at_review, developer_response, sentiment_changed FROM review_versions "
            "WHERE game_id=? AND recommendation_id=? ORDER BY version_no", (g["id"], rid)).fetchall()
        if not rows:
            raise HTTPException(404, "unknown review")
        return [dict(r) for r in rows]

    # -- annotations ---------------------------------------------------------------------------
    def _annotation_body(body: dict) -> tuple[int, str, str, Optional[str]]:
        try:
            event_at = int(body["event_at"])
            kind = str(body.get("kind", "other"))
            title = str(body["title"]).strip()
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "event_at (epoch seconds) and title are required")
        if kind not in store.ANNOTATION_KINDS:
            raise HTTPException(400, f"kind must be one of {store.ANNOTATION_KINDS}")
        if not title or len(title) > 200:
            raise HTTPException(400, "title must be 1-200 characters")
        desc = body.get("description")
        desc = str(desc)[:2000] if desc else None
        return event_at, kind, title, desc

    @app.get("/api/annotations")
    def get_annotations(game_id: Optional[int] = None):
        g = game_or_404(game_id)
        return store.list_annotations(conn(), g["id"])

    @app.post("/api/annotations")
    def add_annotation(body: dict = Body(...), game_id: Optional[int] = None):
        g = game_or_404(game_id)
        ann_id = store.create_annotation(conn(), g["id"], *_annotation_body(body))
        return {"id": ann_id}

    @app.put("/api/annotations/{ann_id}")
    def edit_annotation(ann_id: int, body: dict = Body(...), game_id: Optional[int] = None):
        g = game_or_404(game_id)
        if not store.update_annotation(conn(), ann_id, g["id"], *_annotation_body(body)):
            raise HTTPException(404, "unknown annotation")
        return {"ok": True}

    @app.delete("/api/annotations/{ann_id}")
    def remove_annotation(ann_id: int, game_id: Optional[int] = None):
        g = game_or_404(game_id)
        if not store.delete_annotation(conn(), ann_id, g["id"]):
            raise HTTPException(404, "unknown annotation")
        return {"ok": True}

    # -- settings -------------------------------------------------------------------------------
    @app.put("/api/settings")
    def update_settings(body: dict = Body(...)):
        c = conn()
        with transaction(c):
            gid = store.active_game_id(c)
            if "app_id" in body and body["app_id"]:
                try:
                    app_id = int(body["app_id"])
                    assert app_id > 0
                except (ValueError, TypeError, AssertionError):
                    raise HTTPException(400, "invalid app_id")
                name = str(body.get("name") or f"App {app_id}").strip()[:200]
                gid = store.ensure_game(c, app_id, name)
                c.execute("UPDATE games SET name=? WHERE id=?", (name, gid))
                store.set_setting(c, "active_game_id", str(gid))
            if "twitch_category" in body:
                v = str(body["twitch_category"] or "").strip()[:200]
                if (v or None) != store.get_game(c, gid)["twitch_category"]:
                    # Resolved again on the next Twitch poll. Stored snapshots keep their own category id.
                    store.set_twitch_category(c, gid, v or None, None, None)
            if "launch_at" in body:
                v = body["launch_at"]
                c.execute("UPDATE games SET launch_at=? WHERE id=?", (int(v) if v not in (None, "") else None, gid))
        aggregates.rebuild_all(c, gid, settings.display_tz, settings.ccu_interval, settings.twitch_interval)
        return {"ok": True, "active_game_id": gid}

    @app.post("/api/aggregates/rebuild")
    def rebuild(game_id: Optional[int] = None):
        g = game_or_404(game_id)
        aggregates.rebuild_all(conn(), g["id"], settings.display_tz, settings.ccu_interval, settings.twitch_interval)
        return {"ok": True}

    # -- AI ------------------------------------------------------------------------------------
    @app.get("/api/ai")
    def ai_status(game_id: Optional[int] = None):
        g = game_or_404(game_id)
        return {"enabled": settings.ai_enabled, "available": settings.ai_available, "model": settings.ai_model,
                **ai.theme_overview(conn(), g["id"], g["app_id"])}

    @app.post("/api/ai/analyze")
    def ai_analyze():
        if not settings.ai_available:
            raise HTTPException(400, "AI analysis is not configured (WWP_AI_ENABLED + ANTHROPIC_API_KEY)")
        if not collector.running:
            raise HTTPException(409, "collector is not running in this process")
        collector.wake_ai.set()
        return {"ok": True, "queued": True}

    # -- exports ---------------------------------------------------------------------------------
    def _filename(g: dict, name: str, ext: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"wwp_{g['app_id']}_{name}_{stamp}.{ext}"

    def _delim(delimiter: str) -> str:
        return ";" if delimiter == "semicolon" else ","

    @app.get("/api/export/{dataset}")
    def export(dataset: str, game_id: Optional[int] = None, start: Optional[int] = Query(None, alias="from"),
               end: Optional[int] = Query(None, alias="to"), format: str = "csv", delimiter: str = "comma",
               bucket: Optional[str] = None):
        g = game_or_404(game_id)
        c = conn()
        tz = ZoneInfo(settings.display_tz)
        d = _delim(delimiter)
        if dataset == "zip":
            tmp = _tmp_file(".zip")
            exports.build_zip(c, g["id"], start, end, settings, tmp, d)
            return FileResponse(tmp, media_type="application/zip", filename=_filename(g, "analysis", "zip"),
                                background=BackgroundTask(_unlink, tmp))
        if dataset == "backup":
            tmp = _tmp_file(".sqlite3")
            db.backup_to(tmp)
            return FileResponse(tmp, media_type="application/vnd.sqlite3", filename=_filename(g, "backup", "sqlite3"),
                                background=BackgroundTask(_unlink, tmp))
        if dataset == "reviews" and format == "json":
            body = exports._json(exports.reviews(c, g["id"], start, end, g["app_id"]))
            return _download(body, "application/json", _filename(g, "reviews", "json"))
        if dataset == "review_versions":
            body = exports._json(exports.review_versions(c, g["id"], start, end))
            return _download(body, "application/json", _filename(g, "review_versions", "json"))
        if dataset == "reviews":
            body = exports.reviews_csv(c, g["id"], start, end, g["app_id"], d)
        elif dataset == "ccu_raw":
            body = exports.write_csv(*exports.ccu_raw(c, g["id"], start, end, tz), d)
        elif dataset == "ccu_aggregates":
            body = exports.write_csv(*exports.ccu_aggregates(c, g["id"], start, end, tz, bucket), d)
        elif dataset == "review_summaries":
            body = exports.write_csv(*exports.review_summaries(c, g["id"], start, end, tz), d)
        elif dataset == "review_aggregates":
            body = exports.write_csv(*exports.review_aggregates(c, g["id"], start, end, tz), d)
        elif dataset == "annotations":
            body = exports.write_csv(*exports.annotations(c, g["id"], start, end, tz), d)
        elif dataset == "twitch_snapshots":
            body = exports.write_csv(*exports.twitch_snapshots(c, g["id"], start, end, tz), d)
        elif dataset == "twitch_streams":
            body = exports.write_csv(*exports.twitch_streams(c, g["id"], start, end), d)
        elif dataset == "twitch_stream_observations":
            body = exports.write_csv(*exports.twitch_stream_observations(c, g["id"], start, end), d)
        elif dataset == "twitch_aggregates":
            body = exports.write_csv(*exports.twitch_aggregates(c, g["id"], start, end, tz, bucket), d)
        else:
            raise HTTPException(404, "unknown dataset")
        return _download(body, "text/csv; charset=utf-8", _filename(g, dataset, "csv"))

    def _tmp_file(suffix: str) -> Path:
        p = settings.data_dir / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=suffix, dir=p)
        os.close(fd)
        return Path(name)

    # Remove export leftovers from a previous run (e.g. after a crash).
    for leftover in (settings.data_dir / "tmp").glob("tmp*"):
        _unlink(leftover)

    return app


def _download(body: bytes, media_type: str, filename: str) -> Response:
    return Response(body, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _unlink(path: Path) -> None:
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:  # Windows: file handle may still be closing
            time.sleep(0.2)
