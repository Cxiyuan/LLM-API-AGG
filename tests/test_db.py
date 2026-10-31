from __future__ import annotations

import sqlite3

from catalogwatch.db import Database


def test_workbuddy_tables_and_stats(tmp_path):
    db = Database(tmp_path / "catalog.db")
    try:
        db.conn.execute(
            "INSERT INTO wb_accounts (account_id, provider) VALUES (1, 'workbuddy')"
        )
        db.conn.execute(
            "INSERT INTO wb_models (provider, model_id, first_seen, last_seen, updated_at)"
            " VALUES ('workbuddy', 'm1', '2026-01-01', '2026-01-01', '2026-01-01')"
        )
        db.conn.commit()
        stats = db.stats()
        assert stats["accounts"] == 1
        assert stats["models"] == 1
        assert stats["requests"] == 0
    finally:
        db.close()


def test_old_catalog_tables_are_left_alone(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE providers (id TEXT PRIMARY KEY);
        CREATE TABLE models (provider_id TEXT, model_id TEXT);
        CREATE TABLE provider_keys (provider_id TEXT PRIMARY KEY, ciphertext TEXT);
        INSERT INTO providers (id) VALUES ('nvidia');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        names = {
            row["name"]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"providers", "models", "provider_keys"} <= names
        assert db.conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 1
    finally:
        db.close()


def test_direct_provider_sessions_are_purged(tmp_path):
    db = Database(tmp_path / "catalog.db")
    db.conn.execute(
        "INSERT INTO pool_sessions"
        " (session_id, provider, model_id, created_at, last_used_at, expires_at)"
        " VALUES ('s1', 'nvidia', 'm', '2026-01-01', '2026-01-01', '2099-01-01')"
    )
    db.conn.commit()
    db.close()

    db = Database(tmp_path / "catalog.db")
    try:
        rows = db.conn.execute("SELECT provider FROM pool_sessions").fetchall()
        assert rows == []
    finally:
        db.close()
