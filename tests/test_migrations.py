import hashlib
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import engine
from app.main import app
from app.sqlsplit import split_statements


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr("app.main.ALIASES", {"t": db})
    monkeypatch.setattr(engine, "_locks", {})
    return TestClient(app), db


def mig(v, sql, desc=None):
    desc = desc or f"m{v}"
    return {"version": v, "description": desc, "sql": sql}


def test_split_trigger_and_strings():
    stmts = split_statements("""
        CREATE TABLE a (x TEXT); -- comment; with semi
        INSERT INTO a VALUES ('semi;colon');
        CREATE TRIGGER trg AFTER INSERT ON a BEGIN
            UPDATE a SET x = 'a;b' WHERE x = NEW.x;
            SELECT CASE WHEN 1 THEN 1 END;
        END;
        /* block; comment */ INSERT INTO a VALUES ('z');
    """)
    assert len(stmts) == 4
    assert stmts[2].upper().startswith("CREATE TRIGGER")
    assert "END" in stmts[2].upper()


def test_apply_and_idempotent(client):
    c, db = client
    payload = {"expected_version": 2, "migrations": [
        mig(1, "CREATE TABLE a (id INTEGER PRIMARY KEY, note TEXT);"),
        mig(2, "INSERT INTO a (note) VALUES ('x;y'), ('z'); "
               "CREATE TABLE b (id INTEGER PRIMARY KEY);"),
    ]}
    r = c.post("/db/t/migrate", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["applied_now"] == [1, 2]
    # resubmit same manifest: success, nothing re-executed
    r = c.post("/db/t/migrate", json=payload)
    assert r.json()["applied_now"] == []
    assert r.json()["current_version"] == 2
    status = c.get("/db/t/version").json()
    assert status["current_version"] == 2
    assert status["applied"][0]["sha256"] == hashlib.sha256(
        payload["migrations"][0]["sql"].encode()).hexdigest()


def test_altered_history_rejected(client):
    c, db = client
    p1 = {"expected_version": 1, "migrations": [mig(1, "CREATE TABLE a (x);")]}
    assert c.post("/db/t/migrate", json=p1).status_code == 200
    p2 = {"expected_version": 2, "migrations": [
        mig(1, "CREATE TABLE a (x, y);"),  # tampered
        mig(2, "CREATE TABLE b (x);")]}
    r = c.post("/db/t/migrate", json=p2)
    assert r.status_code == 409
    assert "altered" in r.json()["error"]


def test_missing_history_rejected(client):
    c, db = client
    p1 = {"expected_version": 1, "migrations": [mig(1, "CREATE TABLE a (x);")]}
    c.post("/db/t/migrate", json=p1)
    p2 = {"expected_version": 2, "migrations": [
        mig(1, "CREATE TABLE other (x);"), mig(2, "CREATE TABLE b (x);")]}
    assert c.post("/db/t/migrate", json=p2).status_code == 409


def test_rollback_on_error(client):
    c, db = client
    p = {"expected_version": 2, "migrations": [
        mig(1, "CREATE TABLE a (x);"),
        mig(2, "INSERT INTO a VALUES (1); THIS IS NOT SQL;")]}
    r = c.post("/db/t/migrate", json=p)
    assert r.status_code == 400
    assert r.json()["failed_version"] == 2
    # everything rolled back, including version 1
    assert c.get("/db/t/version").json()["current_version"] == 0
    conn = sqlite3.connect(db)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    assert "a" not in names


def test_forbidden_statements(client):
    c, db = client
    for bad in ["PRAGMA user_version = 3;", "ATTACH 'x.db' AS x;",
                "VACUUM;", "BEGIN; CREATE TABLE a(x); COMMIT;",
                "SELECT 1;", "DETACH main;"]:
        r = c.post("/db/t/migrate", json={
            "expected_version": 1, "migrations": [mig(1, bad)]})
        assert r.status_code == 400, bad
    assert c.get("/db/t/version").json()["current_version"] == 0


def test_cannot_touch_migration_table(client):
    c, db = client
    r = c.post("/db/t/migrate", json={"expected_version": 1, "migrations": [
        mig(1, "DELETE FROM _schema_migrations;")]})
    assert r.status_code == 400
    # trigger indirectly writing the migration table is also blocked
    r = c.post("/db/t/migrate", json={"expected_version": 1, "migrations": [
        mig(1, "CREATE TABLE a (x);"),
        mig(2, "")]})
    assert r.status_code == 400  # blank script
    r = c.post("/db/t/migrate", json={"expected_version": 1, "migrations": [
        mig(1, """
            CREATE TABLE log (m TEXT);
            CREATE TRIGGER evil AFTER INSERT ON log BEGIN
                DELETE FROM _schema_migrations;
            END;
            INSERT INTO log VALUES ('go');
        """)]})
    assert r.status_code == 400
    assert c.get("/db/t/version").json()["current_version"] == 0


def test_duplicate_and_gap_versions(client):
    c, db = client
    r = c.post("/db/t/migrate", json={"expected_version": 2, "migrations": [
        mig(1, "CREATE TABLE a (x);"), mig(1, "CREATE TABLE b (x);")]})
    assert r.status_code == 400
    r = c.post("/db/t/migrate", json={"expected_version": 2, "migrations": [
        mig(2, "CREATE TABLE a (x);"), mig(3, "CREATE TABLE b (x);")]})
    assert r.status_code == 400


def test_expected_version_mismatch(client):
    c, db = client
    r = c.post("/db/t/migrate", json={"expected_version": 5, "migrations": [
        mig(1, "CREATE TABLE a (x);")]})
    assert r.status_code == 409


def test_foreign_key_check(client):
    c, db = client
    r = c.post("/db/t/migrate", json={"expected_version": 1, "migrations": [
        mig(1, """
            CREATE TABLE p (id INTEGER PRIMARY KEY);
            CREATE TABLE ch (id INTEGER PRIMARY KEY,
                             pid INTEGER REFERENCES p(id));
            PRAGMA foreign_keys = OFF;
        """)]})
    assert r.status_code == 400  # PRAGMA refused
    # force an FK violation via disabled checks is not possible; simulate
    # by inserting bad row then checking pragma path directly
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE ch (id INTEGER PRIMARY KEY, "
                 "pid INTEGER REFERENCES p(id))")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO ch VALUES (1, 99)")
    conn.commit()
    conn.close()
    r = c.post("/db/t/migrate", json={"expected_version": 1, "migrations": [
        mig(1, "INSERT INTO p VALUES (1);")]})
    assert r.status_code == 400
    assert "foreign key" in r.json()["error"]


def test_concurrent_submissions(client):
    c, db = client
    payload = {"expected_version": 1, "migrations": [
        mig(1, "CREATE TABLE a (x); INSERT INTO a VALUES (1);")]}
    results = []

    def submit():
        results.append(c.post("/db/t/migrate", json=payload).json())

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    oks = [r for r in results if r.get("ok")]
    assert len(oks) >= 1
    assert c.get("/db/t/version").json()["current_version"] == 1
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM a").fetchone()[0]
    conn.close()
    assert count == 1  # applied exactly once


def test_unknown_alias(client):
    c, _ = client
    assert c.get("/db/nope/version").status_code == 404
