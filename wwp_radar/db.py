"""SQLite storage: connection setup and versioned schema migrations.

All timestamps are stored as INTEGER Unix epoch seconds (UTC by definition).
Conversion to a display timezone happens only in the UI and in exports' *_local
helper columns; exports always carry ISO-8601 UTC strings.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE games (
        id              INTEGER PRIMARY KEY,
        app_id          INTEGER NOT NULL UNIQUE,
        name            TEXT NOT NULL,
        launch_at       INTEGER,              -- configured launch time (UTC epoch), NULL = not set
        created_at      INTEGER NOT NULL,
        monitoring_started_at INTEGER         -- first collection attempt for this game
    );

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- One row per collection attempt (success or failure) for every source.
    CREATE TABLE collection_runs (
        id           INTEGER PRIMARY KEY,
        game_id      INTEGER NOT NULL REFERENCES games(id),
        source       TEXT NOT NULL,           -- ccu | reviews_recent | reviews_updated | review_summary | ai
        started_at   INTEGER NOT NULL,
        finished_at  INTEGER NOT NULL,
        status       TEXT NOT NULL,           -- ok | error | unavailable
        attempts     INTEGER NOT NULL DEFAULT 1,
        http_status  INTEGER,
        items        INTEGER,
        error        TEXT
    );
    CREATE INDEX ix_runs_game_source_time ON collection_runs(game_id, source, started_at);

    CREATE TABLE source_health (
        game_id              INTEGER NOT NULL REFERENCES games(id),
        source               TEXT NOT NULL,
        last_attempt_at      INTEGER,
        last_success_at      INTEGER,
        last_error_at        INTEGER,
        last_error           TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (game_id, source)
    );

    CREATE TABLE checkpoints (
        game_id    INTEGER NOT NULL REFERENCES games(id),
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,             -- JSON
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (game_id, key)
    );

    -- Successful CCU observations only. Failures live in collection_runs.
    CREATE TABLE ccu_observations (
        id           INTEGER PRIMARY KEY,
        game_id      INTEGER NOT NULL REFERENCES games(id),
        observed_at  INTEGER NOT NULL,
        player_count INTEGER NOT NULL CHECK (player_count >= 0),
        UNIQUE (game_id, observed_at)
    );

    -- Current state of each Steam review (one row per recommendation id).
    CREATE TABLE reviews (
        game_id                     INTEGER NOT NULL REFERENCES games(id),
        recommendation_id           TEXT NOT NULL,
        author_steamid              TEXT,
        language                    TEXT,
        review_text                 TEXT,
        voted_up                    INTEGER NOT NULL,
        timestamp_created           INTEGER NOT NULL,
        timestamp_updated           INTEGER,
        playtime_at_review          INTEGER,   -- minutes
        playtime_forever            INTEGER,   -- minutes, at last observation
        steam_purchase              INTEGER,
        received_for_free           INTEGER,
        refunded                    INTEGER,
        written_during_early_access INTEGER,
        primarily_steam_deck        INTEGER,
        votes_up                    INTEGER,
        votes_funny                 INTEGER,
        weighted_vote_score         REAL,
        comment_count               INTEGER,
        developer_response          TEXT,
        timestamp_dev_responded     INTEGER,
        content_hash                TEXT NOT NULL,
        version_count               INTEGER NOT NULL DEFAULT 1,
        first_seen_at               INTEGER NOT NULL,
        last_seen_at                INTEGER NOT NULL,
        PRIMARY KEY (game_id, recommendation_id)
    );
    CREATE INDEX ix_reviews_created ON reviews(game_id, timestamp_created);
    CREATE INDEX ix_reviews_updated ON reviews(game_id, timestamp_updated);
    CREATE INDEX ix_reviews_lang ON reviews(game_id, language);

    -- Immutable history: a new row whenever relevant content/sentiment changes.
    CREATE TABLE review_versions (
        id                          INTEGER PRIMARY KEY,
        game_id                     INTEGER NOT NULL REFERENCES games(id),
        recommendation_id           TEXT NOT NULL,
        version_no                  INTEGER NOT NULL,
        observed_at                 INTEGER NOT NULL,   -- when the monitor saw this version
        timestamp_created           INTEGER NOT NULL,
        timestamp_updated           INTEGER,
        voted_up                    INTEGER NOT NULL,
        review_text                 TEXT,
        language                    TEXT,
        playtime_at_review          INTEGER,
        steam_purchase              INTEGER,
        received_for_free           INTEGER,
        written_during_early_access INTEGER,
        developer_response          TEXT,
        timestamp_dev_responded     INTEGER,
        content_hash                TEXT NOT NULL,
        sentiment_changed           INTEGER NOT NULL DEFAULT 0,  -- voted_up differs from previous version
        raw_json                    TEXT NOT NULL,
        UNIQUE (game_id, recommendation_id, version_no)
    );
    CREATE INDEX ix_versions_observed ON review_versions(game_id, observed_at);
    CREATE INDEX ix_versions_sentiment ON review_versions(game_id, sentiment_changed, timestamp_updated);

    -- Steam query_summary snapshots, one row per population per poll.
    CREATE TABLE review_summary_snapshots (
        id               INTEGER PRIMARY KEY,
        game_id          INTEGER NOT NULL REFERENCES games(id),
        observed_at      INTEGER NOT NULL,
        population       TEXT NOT NULL,        -- stable key, e.g. all_all
        query_params     TEXT NOT NULL,        -- exact JSON query string params
        review_score     INTEGER,
        review_score_desc TEXT,
        total_positive   INTEGER NOT NULL,
        total_negative   INTEGER NOT NULL,
        total_reviews    INTEGER NOT NULL
    );
    CREATE INDEX ix_summary_game_pop_time ON review_summary_snapshots(game_id, population, observed_at);

    -- Rebuildable aggregates -------------------------------------------------
    CREATE TABLE ccu_aggregates (
        game_id          INTEGER NOT NULL REFERENCES games(id),
        bucket           TEXT NOT NULL,        -- 5m | 1h | 1d
        bucket_start     INTEGER NOT NULL,
        bucket_end       INTEGER NOT NULL,
        samples          INTEGER NOT NULL,
        expected_samples REAL NOT NULL,
        coverage         REAL NOT NULL,        -- samples / expected, capped at 1
        min_players      INTEGER,
        max_players      INTEGER,
        mean_players     REAL,
        PRIMARY KEY (game_id, bucket, bucket_start)
    );

    CREATE TABLE review_aggregates (
        game_id               INTEGER NOT NULL REFERENCES games(id),
        bucket                TEXT NOT NULL,   -- 1h | 1d
        bucket_start          INTEGER NOT NULL,
        bucket_end            INTEGER NOT NULL,
        new_positive          INTEGER NOT NULL,
        new_negative          INTEGER NOT NULL,
        new_positive_steam    INTEGER NOT NULL,
        new_negative_steam    INTEGER NOT NULL,
        changed_to_positive   INTEGER NOT NULL,
        changed_to_negative   INTEGER NOT NULL,
        PRIMARY KEY (game_id, bucket, bucket_start)
    );

    CREATE TABLE summary_aggregates (
        game_id        INTEGER NOT NULL REFERENCES games(id),
        bucket         TEXT NOT NULL,          -- 1h | 1d
        population     TEXT NOT NULL,
        bucket_start   INTEGER NOT NULL,
        bucket_end     INTEGER NOT NULL,
        snapshots      INTEGER NOT NULL,
        last_observed_at INTEGER NOT NULL,
        total_positive INTEGER NOT NULL,       -- values of the last snapshot in the bucket
        total_negative INTEGER NOT NULL,
        total_reviews  INTEGER NOT NULL,
        positive_pct   REAL,
        PRIMARY KEY (game_id, bucket, population, bucket_start)
    );

    CREATE TABLE annotations (
        id          INTEGER PRIMARY KEY,
        game_id     INTEGER NOT NULL REFERENCES games(id),
        event_at    INTEGER NOT NULL,
        kind        TEXT NOT NULL,             -- launch | hotfix | patch | stream | marketing | other
        title       TEXT NOT NULL,
        description TEXT,
        created_at  INTEGER NOT NULL,
        updated_at  INTEGER NOT NULL
    );
    CREATE INDEX ix_annotations_game_time ON annotations(game_id, event_at);

    -- Optional AI analysis -----------------------------------------------------
    CREATE TABLE ai_runs (
        id              INTEGER PRIMARY KEY,
        game_id         INTEGER NOT NULL REFERENCES games(id),
        started_at      INTEGER NOT NULL,
        finished_at     INTEGER,
        status          TEXT NOT NULL,         -- running | ok | error | refused
        trigger         TEXT NOT NULL,         -- schedule | manual
        model           TEXT NOT NULL,
        review_count    INTEGER NOT NULL DEFAULT 0,
        window_from     INTEGER,               -- min timestamp_created of analysed reviews
        window_to       INTEGER,
        summary_pl      TEXT,
        themes_json     TEXT,
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        error           TEXT
    );

    -- Which review version (content hash) each run analysed and how it was classified.
    CREATE TABLE ai_review_assignments (
        run_id            INTEGER NOT NULL REFERENCES ai_runs(id),
        game_id           INTEGER NOT NULL,
        recommendation_id TEXT NOT NULL,
        content_hash      TEXT NOT NULL,
        themes            TEXT NOT NULL,       -- JSON list of theme keys (may be empty)
        PRIMARY KEY (run_id, recommendation_id)
    );
    CREATE INDEX ix_ai_assign_review ON ai_review_assignments(game_id, recommendation_id, content_hash);
    """,
}


def connect(path: Path | str, *, check_same_thread: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; rolls back on exceptions."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{current} is newer than this application (v{SCHEMA_VERSION}). Upgrade the app."
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with transaction(conn):
            for stmt in _split_sql(MIGRATIONS[version]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={version}")
    return SCHEMA_VERSION


def _split_sql(script: str) -> list[str]:
    # executescript() would COMMIT our transaction, so split statements manually.
    # The migration scripts contain no semicolons inside string literals.
    parts = []
    for chunk in script.split(";"):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            parts.append(stmt)
    return parts


class Database:
    """Thread-local connections to one SQLite file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = connect(self.path)
        try:
            migrate(conn)
        finally:
            conn.close()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.path)
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def backup_to(self, dest: Path | str) -> None:
        """Consistent online backup using SQLite's backup API.

        The copy runs in a single step (pages=-1) inside one read snapshot. An incremental
        backup would restart every time the collector commits and might never finish.
        """
        src = connect(self.path)
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst, pages=-1)
        finally:
            dst.close()
            src.close()


def now() -> int:
    return int(time.time())
