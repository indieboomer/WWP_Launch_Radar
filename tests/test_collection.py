import httpx
import pytest

from wwp_radar import store
from wwp_radar.collector import Collector, collect_summaries, ensure_updated_walker_initialized, run_walker
from wwp_radar.db import Database, transaction
from wwp_radar.steam import SteamError

from .conftest import APP_ID, make_review


def _collector(settings, db, client):
    return Collector(settings, db, client=client)


def ccu_rows(db, gid):
    return db.conn().execute("SELECT player_count FROM ccu_observations WHERE game_id=?", (gid,)).fetchall()


def runs(db, gid, source="ccu"):
    return [dict(r) for r in db.conn().execute(
        "SELECT status, http_status, error FROM collection_runs WHERE game_id=? AND source=? ORDER BY id", (gid, source))]


# -- CCU ---------------------------------------------------------------------------------------

def test_failed_ccu_requests_are_never_stored_as_zero(settings, db, game_id, client, fake_steam):
    fake_steam.ccu_responses = [(500,), (502,), (503,)]  # all retries fail
    _collector(settings, db, client).poll_ccu_once(game_id, APP_ID)
    assert ccu_rows(db, game_id) == []
    assert runs(db, game_id)[-1]["status"] == "error"
    health = store.source_health(db.conn(), game_id)[0]
    assert health["consecutive_failures"] == 1 and health["last_success_at"] is None


def test_network_exception_is_failure_not_zero(settings, db, game_id, client, fake_steam):
    fake_steam.ccu_responses = [httpx.ConnectTimeout("t")] * 3
    _collector(settings, db, client).poll_ccu_once(game_id, APP_ID)
    assert ccu_rows(db, game_id) == []
    assert runs(db, game_id)[-1]["status"] == "error"


def test_steam_no_data_result_is_unavailable_not_zero(settings, db, game_id, client, fake_steam):
    fake_steam.ccu_responses = ["unavailable"]
    _collector(settings, db, client).poll_ccu_once(game_id, APP_ID)
    assert ccu_rows(db, game_id) == []
    assert runs(db, game_id)[-1]["status"] == "unavailable"


def test_plain_404_without_json_is_an_error(settings, db, game_id, client, fake_steam):
    fake_steam.ccu_responses = [(404,)]
    _collector(settings, db, client).poll_ccu_once(game_id, APP_ID)
    assert ccu_rows(db, game_id) == []
    assert runs(db, game_id)[-1]["status"] == "error"


def test_successful_zero_is_stored_and_retry_recovers(settings, db, game_id, client, fake_steam, monkeypatch):
    clock = iter(range(1_000_000, 1_000_100))
    monkeypatch.setattr("wwp_radar.collector.now", lambda: next(clock))
    fake_steam.ccu_responses = [0, (429,), 17]
    c = _collector(settings, db, client)
    c.poll_ccu_once(game_id, APP_ID)
    c.poll_ccu_once(game_id, APP_ID)  # 429 then retried successfully with 17
    assert [r[0] for r in ccu_rows(db, game_id)] in ([0, 17], [17, 0])
    assert [r["status"] for r in runs(db, game_id)] == ["ok", "ok"]


def test_insert_ccu_rejects_non_observations(db, game_id):
    with pytest.raises(ValueError):
        store.insert_ccu(db.conn(), game_id, 1, None)


def test_non_retryable_status_raises_immediately(client, fake_steam):
    fake_steam.ccu_responses = [(404,)]
    with pytest.raises(SteamError) as e:
        client.current_players(APP_ID)
    assert e.value.http_status == 404 and e.value.attempts == 1


# -- reviews: dedup & versions ---------------------------------------------------------------------

def test_review_dedup_and_version_history(db, game_id):
    conn = db.conn()
    r = make_review(1, 1_000_000)
    with transaction(conn):
        assert store.upsert_review(conn, game_id, r, 1_000_100) == "new"
        assert store.upsert_review(conn, game_id, r, 1_000_200) == "unchanged"
        # volatile counters do not create versions
        assert store.upsert_review(conn, game_id, {**r, "votes_up": 5}, 1_000_300) == "unchanged"
        # text edit -> new version, no sentiment change
        assert store.upsert_review(conn, game_id, {**r, "review": "edited", "timestamp_updated": 1_000_400}, 1_000_500) == "changed"
        # sentiment flip -> new version flagged as sentiment change
        assert store.upsert_review(conn, game_id, {**r, "review": "edited", "voted_up": False,
                                                   "timestamp_updated": 1_000_600}, 1_000_700) == "changed"
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM reviews").fetchone()
    assert row["version_count"] == 3 and row["voted_up"] == 0 and row["votes_up"] == 0  # latest observation
    assert row["first_seen_at"] == 1_000_100 and row["last_seen_at"] == 1_000_700
    assert row["timestamp_created"] == 1_000_000
    versions = conn.execute("SELECT version_no, review_text, voted_up, sentiment_changed, observed_at FROM review_versions "
                            "ORDER BY version_no").fetchall()
    assert [tuple(v) for v in versions] == [
        (1, "review 1", 1, 0, 1_000_100),
        (2, "edited", 1, 0, 1_000_500),
        (3, "edited", 0, 1, 1_000_700),
    ]


# -- pagination, import, downtime recovery ---------------------------------------------------------

def _drain(db, client, gid, kind, max_pages=1, limit=100):
    for _ in range(limit):
        res = run_walker(db, client, gid, APP_ID, kind, max_pages=max_pages, page_size=20)
        if res.done:
            return
    raise AssertionError("walker did not finish")


def test_initial_import_resumes_after_restart_and_is_complete(settings, db, game_id, client, fake_steam):
    base = 1_700_000_000
    for i in range(95):
        fake_steam.add(make_review(i, base + i * 60))
    # Two pages, then "crash"
    run_walker(db, client, game_id, APP_ID, "recent", max_pages=2, page_size=20)
    imp = store.get_checkpoint(db.conn(), game_id, "import")
    assert imp["status"] == "in_progress" and imp["reviews_seen"] == 40 and imp["expected_total"] == 95
    db.close()

    db2 = Database(settings.db_path)  # restart
    fake_steam.requests.clear()
    _drain(db2, client, game_id, "recent")
    # Resumed from the saved cursor: the first request after restart is not the first page.
    assert fake_steam.requests[0]["cursor"] == "c:40"
    assert db2.conn().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 95
    assert store.get_checkpoint(db2.conn(), game_id, "import")["status"] == "complete"
    db2.close()


def test_incremental_catch_up_after_downtime_walks_all_needed_pages(db, game_id, client, fake_steam):
    base = 1_700_000_000
    for i in range(30):
        fake_steam.add(make_review(i, base + i * 3600))
    _drain(db, client, game_id, "recent", max_pages=10)
    # Downtime: 130 new reviews posted (7 pages of 20) - more than one page.
    later = base + 10 * 86400
    for i in range(30, 160):
        fake_steam.add(make_review(i, later + i * 60))
    fake_steam.requests.clear()
    _drain(db, client, game_id, "recent", max_pages=3)  # needs several cycles, checkpointed in between
    assert db.conn().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 160
    # Stopped once it passed the old watermark - did not re-walk the whole history.
    assert len(fake_steam.requests) <= 8


def test_transient_errors_keep_cursor_and_resume(db, game_id, client, fake_steam):
    for i in range(60):
        fake_steam.add(make_review(i, 1_700_000_000 + i))
    run_walker(db, client, game_id, APP_ID, "recent", max_pages=1, page_size=20)
    fake_steam.fail_review_requests = 10  # exhaust retries
    with pytest.raises(SteamError):
        run_walker(db, client, game_id, APP_ID, "recent", max_pages=1, page_size=20)
    fake_steam.fail_review_requests = 0
    pending = store.get_checkpoint(db.conn(), game_id, "walker:recent")["pending"]
    assert pending["cursor"] == "c:20"
    _drain(db, client, game_id, "recent")
    assert db.conn().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 60


def test_updated_walker_reconciles_edits_of_old_reviews(db, game_id, client, fake_steam):
    base = 1_700_000_000
    for i in range(50):
        fake_steam.add(make_review(i, base + i * 3600))
    _drain(db, client, game_id, "recent", max_pages=10)
    assert ensure_updated_walker_initialized(db.conn(), game_id)
    # An old review is edited much later: it does not appear at the top of filter=recent.
    import_start = store.get_checkpoint(db.conn(), game_id, "import")["started_at"]
    fake_steam.add(make_review(3, base + 3 * 3600, up=False, text="now negative", updated=import_start + 500))
    _drain(db, client, game_id, "recent", max_pages=10)
    row = db.conn().execute("SELECT voted_up, version_count FROM reviews WHERE recommendation_id='1003'").fetchone()
    assert tuple(row) == (1, 1)  # not yet seen by the 'recent' walker
    _drain(db, client, game_id, "updated", max_pages=10)
    row = db.conn().execute("SELECT voted_up, version_count FROM reviews WHERE recommendation_id='1003'").fetchone()
    assert tuple(row) == (0, 2)


def test_missing_review_is_not_deleted(db, game_id, client, fake_steam):
    for i in range(10):
        fake_steam.add(make_review(i, 1_700_000_000 + i))
    _drain(db, client, game_id, "recent", max_pages=10)
    del fake_steam.reviews["1005"]
    _drain(db, client, game_id, "recent", max_pages=10)
    assert db.conn().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 10


def test_summary_snapshots_keep_populations_separate(db, game_id, client, fake_steam):
    fake_steam.add(make_review(1, 1, steam=True))
    fake_steam.add(make_review(2, 2, steam=False, up=False))
    assert collect_summaries(db, client, game_id, APP_ID) == 2
    rows = {r["population"]: dict(r) for r in db.conn().execute("SELECT * FROM review_summary_snapshots")}
    assert rows["all_all"]["total_reviews"] == 2
    assert rows["all_steam"]["total_reviews"] == 1
    assert '"purchase_type": "steam"' in rows["all_steam"]["query_params"]
