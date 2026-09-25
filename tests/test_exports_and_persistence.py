import csv
import io
import json
import sqlite3
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from wwp_radar import exports, store
from wwp_radar.app import create_app
from wwp_radar.config import Settings, validate_exposure
from wwp_radar.db import SCHEMA_VERSION, Database, transaction

from .conftest import APP_ID, make_review

B = 1_790_000_000  # 2026-09-21, keeps rebuilt aggregates small


def seed(db, gid):
    conn = db.conn()
    with transaction(conn):
        conn.execute("UPDATE games SET monitoring_started_at=? WHERE id=?", (B + 1000, gid))
        store.insert_ccu(conn, gid, B + 1000, 5)
        store.insert_ccu(conn, gid, B + 1060, 0)
        store.insert_ccu(conn, gid, B + 5000, 9)
        store.upsert_review(conn, gid, make_review(1, B + 2000, text='=cmd|"/c calc"!A1\nline two, "quoted"'), B + 9000)
        store.upsert_review(conn, gid, make_review(2, B + 8000, up=False), B + 9000)
        store.upsert_review(conn, gid, make_review(1, B + 2000, text="edited later", updated=B + 20000), B + 20000)
    store.create_annotation(conn, gid, B + 1500, "launch", "@Launch", None)


def parse_csv(data: bytes) -> list[dict]:
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


def test_csv_formula_injection_and_multiline_quoting(db, game_id):
    seed(db, game_id)
    rows = parse_csv(exports.reviews_csv(db.conn(), game_id, None, None, APP_ID))
    assert len(rows) == 2
    # current text of review 1 is "edited later"; check an injected text via a fresh review
    conn = db.conn()
    with transaction(conn):
        store.upsert_review(conn, game_id, make_review(3, 3000, text='=HYPERLINK("x")\nsecond, line'), 9000)
    rows = {r["recommendation_id"]: r for r in parse_csv(exports.reviews_csv(conn, game_id, None, None, APP_ID))}
    assert rows["1003"]["review_text"] == "'=HYPERLINK(\"x\")\nsecond, line"
    # JSON preserves the original text
    js = {r["recommendation_id"]: r for r in exports.reviews(conn, game_id, None, None, APP_ID)}
    assert js["1003"]["review_text"] == '=HYPERLINK("x")\nsecond, line'
    ann = parse_csv(exports.write_csv(*exports.annotations(conn, game_id, None, None, exports.ZoneInfo("Europe/Warsaw"))))
    assert ann[0]["title"] == "'@Launch"


def test_semicolon_delimiter(db, game_id):
    seed(db, game_id)
    data = exports.write_csv(*exports.ccu_raw(db.conn(), game_id, None, None, exports.ZoneInfo("UTC")), ";")
    assert data.decode("utf-8-sig").splitlines()[0] == "observed_at_utc;observed_at_local;player_count"


def test_date_filter_semantics(db, game_id):
    seed(db, game_id)
    conn = db.conn()
    tz = exports.ZoneInfo("UTC")
    # CCU raw filtered by observation time, half-open [from, to)
    _, rows = exports.ccu_raw(conn, game_id, B + 1000, B + 5000, tz)
    assert [r[2] for r in rows] == [5, 0]  # zero is a real observation; 5000 excluded
    assert rows[0][0] == "2026-09-21T14:30:00Z"
    # Reviews filtered by creation time (review 1 created at 2000, observed at 9000)
    assert [r["recommendation_id"] for r in exports.reviews(conn, game_id, B + 1500, B + 2500, APP_ID)] == ["1001"]
    assert exports.reviews(conn, game_id, B + 8500, B + 9500, APP_ID) == []
    # Versions filtered by observation time: only the edit observed at 20000
    v = exports.review_versions(conn, game_id, B + 15000, B + 25000)
    assert [(x["recommendation_id"], x["version_no"]) for x in v] == [("1001", 2)]


def test_zip_contains_all_datasets_and_metadata(db, game_id, settings, tmp_path):
    seed(db, game_id)
    dest = tmp_path / "out.zip"
    exports.build_zip(db.conn(), game_id, None, None, settings, dest)
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        assert {"ccu_raw.csv", "ccu_aggregates.csv", "reviews.csv", "reviews.json", "review_versions.json",
                "review_summaries.csv", "review_aggregates.csv", "annotations.csv", "metadata.json"} <= names
        meta = json.loads(zf.read("metadata.json"))
        versions = json.loads(zf.read("review_versions.json"))
    assert meta["game"]["app_id"] == APP_ID
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["datasets"]["reviews"]["date_filter_field"] == "timestamp_created"
    assert meta["datasets"]["review_versions"]["date_filter_field"] == "observed_at"
    assert meta["collection"]["ccu_gap_count_in_range"] >= 1  # gap between B+1060 and B+5000
    assert meta["collection"]["review_import"]["status"] == "not_started"
    assert "limitations" in meta and "ANTHROPIC" not in json.dumps(meta).upper()
    assert versions[0]["steam_raw"]["recommendationid"] == "1001"


def test_persistence_across_restarts_and_migration_idempotent(settings, db, game_id):
    seed(db, game_id)
    with transaction(db.conn()):
        store.set_checkpoint(db.conn(), game_id, "walker:recent", {"watermark": 123, "pending": {"cursor": "c:40"}})
    db.close()
    db2 = Database(settings.db_path)  # re-runs migrations: must be a no-op
    c = db2.conn()
    assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert c.execute("SELECT COUNT(*) FROM ccu_observations").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM review_versions").fetchone()[0] == 3
    assert store.get_checkpoint(c, game_id, "walker:recent")["pending"]["cursor"] == "c:40"
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db2.close()


def test_backup_is_consistent_while_writes_continue(settings, db, game_id, tmp_path):
    seed(db, game_id)
    stop = threading.Event()

    def writer():
        d = Database(settings.db_path)
        i = 10_000
        while not stop.is_set():
            with transaction(d.conn()):
                store.insert_ccu(d.conn(), game_id, i, i % 100)
            i += 1
        d.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        dest = tmp_path / "backup.sqlite3"
        db.backup_to(dest)
    finally:
        stop.set()
        t.join()
    b = sqlite3.connect(dest)
    assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert b.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert b.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
    assert b.execute("SELECT COUNT(*) FROM ccu_observations").fetchone()[0] >= 3
    b.close()
    # The backup opens as a working application database.
    assert Database(dest).conn().execute("SELECT COUNT(*) FROM review_versions").fetchone()[0] == 3


def test_app_id_change_keeps_histories_separate(settings, db, game_id):
    seed(db, game_id)
    db.close()
    app = create_app(settings, start_collector=False)
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"app_id": 730, "name": "Other"}).status_code == 200
        st = c.get("/api/status").json()
        assert st["game"]["app_id"] == 730 and len(st["games"]) == 2
        assert c.get("/api/reviews").json()["total"] == 0
        assert c.get(f"/api/reviews?game_id={game_id}").json()["total"] == 2
        r = c.get(f"/api/export/ccu_raw?game_id={game_id}")
        assert len(parse_csv(r.content)) == 3


# -- API / dashboard ----------------------------------------------------------------------------------

def test_api_endpoints_and_exports(settings, db, game_id):
    seed(db, game_id)
    db.close()
    app = create_app(settings, start_collector=False)
    with TestClient(app) as c:
        assert c.get("/healthz").json()["status"] == "ok"
        ov = c.get("/api/overview").json()
        assert ov["ccu"]["current"]["player_count"] == 9
        assert ov["ccu"]["peak"]["player_count"] == 9
        assert ov["ccu"]["change"]["15"] is None  # no sample ~15 min earlier -> "unavailable"
        assert ov["summary"] is None
        feed = c.get("/api/reviews?sentiment=negative").json()
        assert feed["total"] == 1
        assert c.get("/api/reviews?q=edited").json()["total"] == 1
        assert c.get("/api/reviews?edited=true").json()["total"] == 1
        assert len(c.get("/api/reviews/1001/versions").json()) == 2
        series = c.get(f"/api/ccu?from={B + 900}&to={B + 6000}").json()
        assert series["resolution"] == "raw" and None in series["value"]  # gap marker, not zero
        a = c.post("/api/annotations", json={"event_at": B + 4000, "kind": "hotfix", "title": "Hotfix 1"}).json()
        assert c.put(f"/api/annotations/{a['id']}", json={"event_at": B + 4100, "kind": "hotfix", "title": "Hotfix 1b"}).status_code == 200
        assert c.post("/api/annotations", json={"event_at": 1, "kind": "bogus", "title": "x"}).status_code == 400
        assert c.delete(f"/api/annotations/{a['id']}").status_code == 200
        for ds in ("ccu_raw", "ccu_aggregates", "reviews", "review_summaries", "review_aggregates", "annotations"):
            assert c.get(f"/api/export/{ds}").status_code == 200
        assert json.loads(c.get("/api/export/reviews?format=json").content)[0]["recommendation_id"] == "1001"
        z = c.get("/api/export/zip")
        assert z.status_code == 200 and zipfile.ZipFile(io.BytesIO(z.content)).testzip() is None
        bk = c.get("/api/export/backup")
        assert bk.content[:16] == b"SQLite format 3\x00"


def test_remote_bind_requires_auth(tmp_path):
    with pytest.raises(SystemExit):
        validate_exposure(Settings(data_dir=tmp_path, host="0.0.0.0"))
    validate_exposure(Settings(data_dir=tmp_path, host="0.0.0.0", dashboard_password="pw"))
    validate_exposure(Settings(data_dir=tmp_path, host="0.0.0.0", trust_proxy_auth=True))


def test_password_protects_api_exports_and_settings(tmp_path):
    s = Settings(data_dir=tmp_path, dashboard_password="s3cret")
    app = create_app(s, start_collector=False)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        for path in ("/api/status", "/api/export/backup", "/api/export/zip"):
            assert c.get(path).status_code == 401
        assert c.put("/api/settings", json={"launch_at": 1}).status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 303
        c.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert c.get("/api/status").status_code == 401
        r = c.post("/login", data={"password": "s3cret"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/api/status").status_code == 200


def test_launch_at_parsing():
    from wwp_radar.config import parse_launch_at
    assert parse_launch_at("", "Europe/Warsaw") is None
    # Without an offset the value is read in the display timezone (CEST = UTC+2 in October before DST ends).
    assert parse_launch_at("2026-10-15 14:00", "Europe/Warsaw") == parse_launch_at("2026-10-15T12:00:00Z", "UTC")
    assert parse_launch_at("2026-10-15T14:00:00+02:00", "Europe/Warsaw") == parse_launch_at("2026-10-15T12:00Z", "UTC")
    with pytest.raises(ValueError, match="WWP_LAUNCH_AT"):
        parse_launch_at("14:00", "Europe/Warsaw")
