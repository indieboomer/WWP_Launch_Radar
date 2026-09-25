"""Background collection: CCU polling, review import/incremental sync, summary snapshots,
aggregate maintenance. Runs in threads owned by the single application process and is
guarded by an OS-level lock file so that only one collector can write per data directory.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import aggregates, store
from .config import Settings
from .db import Database, now, transaction
from .steam import ShutdownRequested, SteamClient, SteamError

if TYPE_CHECKING:
    from .twitch import TwitchClient

log = logging.getLogger(__name__)

# Review query used for the review record walkers: every language, every purchase type,
# no off-topic (review bomb) filtering, so that the stored population is as complete as
# Steam allows. Filters are applied later, on stored fields.
REVIEW_BASE_PARAMS = {
    "language": "all",
    "purchase_type": "all",
    "review_type": "all",
    "filter_offtopic_activity": 0,
}

# Steam review-summary populations snapshotted every review poll. Never mixed with each other.
SUMMARY_POPULATIONS: dict[str, dict] = {
    "all_all": {
        "label": "Wszystkie języki, wszystkie typy pozyskania (Steam: domyślny filtr off-topic)",
        "params": {"language": "all", "purchase_type": "all", "review_type": "all", "filter": "recent", "num_per_page": 0},
    },
    "all_steam": {
        "label": "Wszystkie języki, tylko zakupy na Steam (Steam: domyślny filtr off-topic)",
        "params": {"language": "all", "purchase_type": "steam", "review_type": "all", "filter": "recent", "num_per_page": 0},
    },
}

WALKER_SORT_FIELD = {"recent": "timestamp_created", "updated": "timestamp_updated"}
OVERLAP_SECONDS = 3600          # re-scan this far behind the watermark to absorb clock skew / late indexing
PAGES_PER_CYCLE = 10            # checkpoint granularity; the walker resumes right away if pending
MAX_CURSOR_FAILURES = 3


class LockError(Exception):
    pass


class CollectorLock:
    """Exclusive, process-level lock on <data_dir>/<db>.collector.lock."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise LockError(f"Another collector already holds {self.path}") from e
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


# ---------------------------------------------------------------------------------------
# Review walkers
# ---------------------------------------------------------------------------------------

@dataclass
class WalkResult:
    pages: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    done: bool = False
    attempts: int = 0


def walker_key(kind: str) -> str:
    return f"walker:{kind}"


def run_walker(db: Database, client: SteamClient, game_id: int, app_id: int, kind: str,
               max_pages: int = PAGES_PER_CYCLE, page_size: int = 100,
               clock: Callable[[], int] = now) -> WalkResult:
    """Advance a review walker by up to max_pages pages.

    kind='recent' walks reviews newest-created first; kind='updated' newest-updated first.
    State (checkpoint) = {"watermark": int|None, "pending": {...}|None}. A walk starts from the
    newest review and continues page by page until it reaches reviews older than
    watermark - OVERLAP (or the end of the list when watermark is None, i.e. the initial import).
    The cursor is checkpointed after every page, so a crash or downtime resumes where it stopped;
    the watermark only advances when a walk completes, so no page is ever skipped.
    """
    conn = db.conn()
    sort_field = WALKER_SORT_FIELD[kind]
    key = walker_key(kind)
    state = store.get_checkpoint(conn, game_id, key, {"watermark": None, "pending": None})
    pending = state.get("pending")
    if pending is None:
        wm = state.get("watermark")
        pending = {
            "cursor": "*",
            "stop_at": (wm - OVERLAP_SECONDS) if wm is not None else None,
            "new_watermark": None,
            "started_at": clock(),
            "pages": 0,
            "reviews_seen": 0,
            "expected_total": None,
            "failures": 0,
        }
    result = WalkResult()
    params = {**REVIEW_BASE_PARAMS, "filter": kind, "num_per_page": page_size}

    for _ in range(max_pages):
        try:
            data, attempts = client.reviews_page(app_id, params, pending["cursor"])
        except SteamError:
            pending["failures"] = pending.get("failures", 0) + 1
            if pending["failures"] >= MAX_CURSOR_FAILURES and pending["cursor"] != "*":
                # Cursor may have expired: restart this walk from the top, keeping its stop point.
                log.warning("Walker %s: resetting cursor after repeated failures", kind)
                pending["cursor"] = "*"
                pending["failures"] = 0
            with transaction(conn):
                store.set_checkpoint(conn, game_id, key, {**state, "pending": pending})
            raise
        result.attempts += attempts
        observed = clock()
        reviews = data.get("reviews") or []
        next_cursor = data.get("cursor")
        qs = data.get("query_summary") or {}
        with transaction(conn):
            for raw in reviews:
                outcome = store.upsert_review(conn, game_id, raw, observed)
                setattr(result, outcome, getattr(result, outcome) + 1)
            if pending["cursor"] == "*" and "total_reviews" in qs:
                pending["expected_total"] = int(qs["total_reviews"])
            sort_values = [int(r.get(sort_field) or r.get("timestamp_created") or 0) for r in reviews]
            if sort_values:
                top = max(sort_values)
                if pending["new_watermark"] is None or top > pending["new_watermark"]:
                    pending["new_watermark"] = top
            pending["pages"] += 1
            pending["reviews_seen"] += len(reviews)
            pending["failures"] = 0
            result.pages += 1

            reached_stop = pending["stop_at"] is not None and sort_values and min(sort_values) < pending["stop_at"]
            end_of_list = not reviews or not next_cursor or next_cursor == pending["cursor"]
            if reached_stop or end_of_list:
                prev_wm = state.get("watermark")
                new_wm = pending["new_watermark"]
                if new_wm is None:
                    new_wm = prev_wm if prev_wm is not None else pending["started_at"]
                elif prev_wm is not None:
                    new_wm = max(new_wm, prev_wm)
                state = {
                    "watermark": new_wm,
                    "pending": None,
                    "last_completed_at": clock(),
                    "last_walk_pages": pending["pages"],
                    "last_walk_reviews": pending["reviews_seen"],
                    "full_walk_completed": state.get("full_walk_completed") or pending["stop_at"] is None,
                }
                if kind == "recent" and pending["stop_at"] is None:
                    # The initial import is complete.
                    store.set_checkpoint(conn, game_id, "import", {
                        "status": "complete",
                        "started_at": pending["started_at"],
                        "completed_at": clock(),
                        "reviews_seen": pending["reviews_seen"],
                        "expected_total": pending["expected_total"],
                        "pages": pending["pages"],
                    })
                store.set_checkpoint(conn, game_id, key, state)
                result.done = True
                return result
            pending["cursor"] = next_cursor
            state = {**state, "pending": pending}
            if kind == "recent" and pending["stop_at"] is None:
                store.set_checkpoint(conn, game_id, "import", {
                    "status": "in_progress",
                    "started_at": pending["started_at"],
                    "reviews_seen": pending["reviews_seen"],
                    "expected_total": pending["expected_total"],
                    "pages": pending["pages"],
                })
            store.set_checkpoint(conn, game_id, key, state)
    return result


def ensure_updated_walker_initialized(conn, game_id: int) -> bool:
    """The 'updated' walker starts after the import, from the moment the import began."""
    imp = store.get_checkpoint(conn, game_id, "import")
    if not imp or imp.get("status") != "complete":
        return False
    st = store.get_checkpoint(conn, game_id, walker_key("updated"))
    if st is None:
        with transaction(conn):
            store.set_checkpoint(conn, game_id, walker_key("updated"), {"watermark": imp["started_at"], "pending": None})
    return True


def collect_summaries(db: Database, client: SteamClient, game_id: int, app_id: int) -> int:
    conn = db.conn()
    stored = 0
    for pop, spec in SUMMARY_POPULATIONS.items():
        started = now()
        try:
            data, attempts = client.reviews_page(app_id, spec["params"], "*")
            qs = data.get("query_summary") or {}
            if not all(k in qs for k in ("total_positive", "total_negative", "total_reviews")):
                raise SteamError("query_summary missing totals", 200, attempts)
        except SteamError as e:
            with transaction(conn):
                store.record_run(conn, game_id, "review_summary", started, "error",
                                 attempts=e.attempts, http_status=e.http_status, error=f"{pop}: {e}")
            continue
        with transaction(conn):
            store.insert_summary(conn, game_id, now(), pop, spec["params"], qs)
            store.record_run(conn, game_id, "review_summary", started, "ok", attempts=attempts, http_status=200, items=1)
        stored += 1
    return stored


# ---------------------------------------------------------------------------------------
# Collector service
# ---------------------------------------------------------------------------------------

@dataclass
class Collector:
    settings: Settings
    db: Database
    client: SteamClient | None = None
    twitch_client: "TwitchClient | None" = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)
    lock: CollectorLock | None = None
    running: bool = False
    lock_error: str | None = None
    heartbeat: dict[str, int] = field(default_factory=dict)
    wake_ai: threading.Event = field(default_factory=threading.Event)

    def start(self) -> bool:
        self.lock = CollectorLock(self.settings.db_path.with_suffix(".collector.lock"))
        try:
            self.lock.acquire()
        except LockError as e:
            self.lock_error = str(e)
            log.error("%s - dashboard will run without a collector", e)
            return False
        if self.client is None:
            self.client = SteamClient(
                timeout=self.settings.http_timeout,
                max_retries=self.settings.http_max_retries,
                max_concurrency=self.settings.steam_max_concurrency,
                min_request_gap=self.settings.steam_min_request_gap,
                stop_event=self.stop_event,
            )
        targets = [("ccu", self._ccu_loop), ("reviews", self._review_loop), ("aggregates", self._aggregate_loop)]
        if self.settings.twitch_available:
            from . import twitch
            if self.twitch_client is None:
                self.twitch_client = twitch.TwitchClient(
                    self.settings.twitch_client_id, self.settings.twitch_client_secret,
                    timeout=self.settings.http_timeout, max_retries=self.settings.http_max_retries,
                    max_concurrency=1, min_request_gap=0.2, stop_event=self.stop_event,
                )
            targets.append(("twitch", lambda: twitch.twitch_loop(self)))
        if self.settings.ai_available:
            from . import ai
            targets.append(("ai", lambda: ai.ai_loop(self)))
        for name, target in targets:
            t = threading.Thread(target=self._guard(name, target), name=f"collector-{name}", daemon=True)
            t.start()
            self.threads.append(t)
        self.running = True
        log.info("Collector started (CCU every %ss, reviews every %ss, Twitch %s)", self.settings.ccu_interval,
                 self.settings.review_interval,
                 f"every {self.settings.twitch_interval}s" if self.settings.twitch_available else "not configured")
        return True

    def stop(self, timeout: float = 15.0) -> None:
        self.stop_event.set()
        self.wake_ai.set()
        deadline = time.monotonic() + timeout
        for t in self.threads:
            t.join(max(0.1, deadline - time.monotonic()))
        if self.client:
            self.client.close()
        if self.twitch_client:
            self.twitch_client.close()
        if self.lock:
            self.lock.release()
        self.running = False
        log.info("Collector stopped")

    def _guard(self, name: str, target: Callable[[], None]) -> Callable[[], None]:
        def run():
            while not self.stop_event.is_set():
                try:
                    target()
                    return
                except ShutdownRequested:
                    return
                except Exception:  # keep the thread alive; log and back off
                    log.exception("Collector thread %s crashed; restarting in 10s", name)
                    if self.stop_event.wait(10):
                        return
        return run

    def _game(self) -> tuple[int, int] | None:
        conn = self.db.conn()
        gid = store.active_game_id(conn)
        if gid is None:
            return None
        g = store.get_game(conn, gid)
        return (g["id"], g["app_id"]) if g else None

    # -- CCU -------------------------------------------------------------------------
    def _ccu_loop(self) -> None:
        interval = self.settings.ccu_interval
        next_at = time.time()
        while not self.stop_event.is_set():
            self.heartbeat["ccu"] = now()
            game = self._game()
            if game:
                self.poll_ccu_once(*game)
            next_at += interval
            delay = next_at - time.time()
            if delay < 0:  # fell behind (sleep/suspend): realign without a burst of catch-up requests
                next_at = time.time() + interval
                delay = interval
            if self.stop_event.wait(delay):
                return

    def poll_ccu_once(self, game_id: int, app_id: int) -> None:
        conn = self.db.conn()
        started = now()
        with transaction(conn):
            store.mark_monitoring_started(conn, game_id, started)
        try:
            res = self.client.current_players(app_id)
        except SteamError as e:
            with transaction(conn):
                store.record_run(conn, game_id, "ccu", started, "error", attempts=e.attempts,
                                 http_status=e.http_status, error=str(e))
            return
        with transaction(conn):
            if res.status == "ok":
                store.insert_ccu(conn, game_id, now(), res.player_count)
                store.record_run(conn, game_id, "ccu", started, "ok", attempts=res.attempts, http_status=res.http_status, items=1)
            else:
                store.record_run(conn, game_id, "ccu", started, "unavailable", attempts=res.attempts,
                                 http_status=res.http_status, error=res.detail)

    # -- reviews -----------------------------------------------------------------------
    def _review_loop(self) -> None:
        last_summary = 0.0
        while not self.stop_event.is_set():
            self.heartbeat["reviews"] = now()
            game = self._game()
            more_pending = False
            if game:
                game_id, app_id = game
                if time.time() - last_summary >= self.settings.review_interval:
                    collect_summaries(self.db, self.client, game_id, app_id)
                    last_summary = time.time()
                more_pending |= self._walk(game_id, app_id, "recent")
                if ensure_updated_walker_initialized(self.db.conn(), game_id):
                    more_pending |= self._walk(game_id, app_id, "updated")
            # Resume quickly while an import/catch-up is pending; otherwise wait the poll interval.
            wait = 2 if more_pending else self.settings.review_interval
            if self.stop_event.wait(wait):
                return

    def _walk(self, game_id: int, app_id: int, kind: str) -> bool:
        conn = self.db.conn()
        started = now()
        source = f"reviews_{kind}"
        try:
            res = run_walker(self.db, self.client, game_id, app_id, kind, page_size=self.settings.review_page_size)
        except SteamError as e:
            with transaction(conn):
                store.record_run(conn, game_id, source, started, "error", attempts=e.attempts,
                                 http_status=e.http_status, error=str(e))
            return False
        with transaction(conn):
            store.record_run(conn, game_id, source, started, "ok", attempts=res.attempts, http_status=200,
                             items=res.new + res.changed)
        return not res.done

    # -- aggregates --------------------------------------------------------------------
    def _aggregate_loop(self) -> None:
        tz = self.settings.display_tz
        game = self._game()
        if game:
            aggregates.rebuild_all(self.db.conn(), game[0], tz, self.settings.ccu_interval, self.settings.twitch_interval)
        while not self.stop_event.wait(60):
            self.heartbeat["aggregates"] = now()
            game = self._game()
            if game:
                aggregates.refresh_recent(self.db.conn(), game[0], tz, self.settings.ccu_interval,
                                          self.settings.twitch_interval)
