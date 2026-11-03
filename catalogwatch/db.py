"""SQLite access for the unified model aggregation gateway.

The free-LLM catalog tables from earlier releases are deliberately not created
or read here. Existing databases keep those tables untouched; only WorkBuddy
pool tables are migrated and used.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .workbuddy.providers import ROUTABLE_PROVIDER_ORDER

WORKBUDDY_SCHEMA = """
CREATE TABLE IF NOT EXISTS wb_accounts (
    account_id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'workbuddy',
    name TEXT,
    nickname TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    present INTEGER NOT NULL DEFAULT 1,
    weight INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    credit_limit REAL NOT NULL DEFAULT 0,
    credit_baseline REAL NOT NULL DEFAULT 0,
    total_credits REAL NOT NULL DEFAULT 0,
    available_credits REAL,
    packages_json TEXT,
    token_expires_at INTEGER,
    last_synced_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS wb_account_state (
    account_id INTEGER PRIMARY KEY,
    health TEXT NOT NULL DEFAULT 'unknown',
    cooldown_until TEXT,
    free_exhausted_until TEXT,
    daily_date TEXT,
    daily_requests INTEGER NOT NULL DEFAULT 0,
    daily_tokens INTEGER NOT NULL DEFAULT 0,
    daily_credits REAL NOT NULL DEFAULT 0,
    free_requests_today INTEGER NOT NULL DEFAULT 0,
    paid_requests_today INTEGER NOT NULL DEFAULT 0,
    free_429_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_status_code INTEGER,
    last_error TEXT,
    last_429_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS wb_account_caps (
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL DEFAULT '*',
    daily_request_cap INTEGER NOT NULL DEFAULT 0,
    daily_credit_cap REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id, model_id)
);

CREATE TABLE IF NOT EXISTS wb_models (
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    name TEXT,
    context_length INTEGER,
    max_output INTEGER,
    base_credits REAL,
    credits_raw TEXT,
    capabilities_json TEXT,
    tags_json TEXT,
    official INTEGER NOT NULL DEFAULT 0,
    present INTEGER NOT NULL DEFAULT 1,
    source_identity TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, model_id)
);
CREATE INDEX IF NOT EXISTS idx_wb_models_present ON wb_models(present);

CREATE TABLE IF NOT EXISTS wb_promotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    label TEXT,
    color TEXT,
    factor REAL,
    priority INTEGER NOT NULL DEFAULT 0,
    active_only INTEGER NOT NULL DEFAULT 0,
    schedule_json TEXT,
    text_zh TEXT,
    text_en TEXT,
    source_identity TEXT,
    fetched_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wb_promotions_unique
    ON wb_promotions(provider, model_id, COALESCE(label,''), COALESCE(factor,-999999), COALESCE(schedule_json,''));
CREATE INDEX IF NOT EXISTS idx_wb_promotions_model ON wb_promotions(provider, model_id);

CREATE TABLE IF NOT EXISTS wb_config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_id INTEGER,
    identity TEXT,
    fetched_at TEXT NOT NULL,
    status TEXT,
    model_count INTEGER NOT NULL DEFAULT 0,
    promotion_count INTEGER NOT NULL DEFAULT 0,
    payload_hash TEXT,
    payload_json TEXT,
    warning TEXT
);

CREATE TABLE IF NOT EXISTS manual_model_costs (
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    base_credits REAL,
    factor REAL,
    daily_windows_json TEXT,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    valid_from TEXT,
    valid_until TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, model_id)
);

CREATE TABLE IF NOT EXISTS credit_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    available_total REAL,
    total_dosage REAL,
    expiring_7d REAL,
    expiring_30d REAL,
    package_count INTEGER,
    packages_json TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_credit_snapshots_account ON credit_snapshots(account_id, id DESC);

CREATE TABLE IF NOT EXISTS pool_sessions (
    session_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    account_id INTEGER,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pool_sessions_expiry ON pool_sessions(expires_at);

CREATE TABLE IF NOT EXISTS pool_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    kind TEXT NOT NULL DEFAULT 'agent',
    allowed_models_json TEXT,
    daily_request_limit INTEGER NOT NULL DEFAULT 0,
    daily_credit_limit REAL NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    total_requests INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_credits REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pool_key_daily_usage (
    pool_key_id INTEGER NOT NULL,
    usage_date TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    credits REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(pool_key_id, usage_date)
);

CREATE TABLE IF NOT EXISTS pool_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    session_id TEXT,
    pool_key_id INTEGER,
    endpoint TEXT,
    model_requested TEXT,
    model_routed TEXT,
    provider TEXT,
    bridge_account_id INTEGER,
    intent_json TEXT,
    stream INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    credits REAL,
    credits_estimated INTEGER NOT NULL DEFAULT 0,
    effective_credits REAL,
    cost_source TEXT,
    status_code INTEGER,
    finish_reason TEXT,
    error_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    routing_json TEXT,
    created_at TEXT NOT NULL,
    reconciled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pool_requests_created ON pool_requests(created_at);
CREATE INDEX IF NOT EXISTS idx_pool_requests_account ON pool_requests(bridge_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pool_requests_key ON pool_requests(pool_key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pool_requests_model ON pool_requests(model_routed);
CREATE INDEX IF NOT EXISTS idx_pool_requests_status ON pool_requests(status_code);

CREATE TABLE IF NOT EXISTS pool_request_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    account_id INTEGER,
    model_id TEXT,
    provider TEXT,
    status_code INTEGER,
    error_code TEXT,
    started_at TEXT,
    duration_ms INTEGER,
    credits REAL,
    UNIQUE(request_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS pool_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Per-account check-in overrides. A row means "this account does not inherit
-- the global rule"; enabled=0 is an explicit opt-out.
CREATE TABLE IF NOT EXISTS wb_checkin_rules (
    account_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    hour INTEGER NOT NULL DEFAULT 9,
    minute INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Last auto/manual check-in attempt per account, used to run once per day and
-- to show a status without hitting the bridge on every page render.
CREATE TABLE IF NOT EXISTS wb_checkin_state (
    account_id INTEGER PRIMARY KEY,
    last_date TEXT,
    last_status TEXT,
    last_message TEXT,
    last_attempt_at TEXT,
    checked_at TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Serialize pool writes; WAL + busy_timeout covers concurrent readers.
        self.write_lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(WORKBUDDY_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(pool_requests)")
        }
        if columns and "session_id" not in columns:
            self.conn.execute("ALTER TABLE pool_requests ADD COLUMN session_id TEXT")
        if columns and "cache_hit_tokens" not in columns:
            self.conn.execute(
                "ALTER TABLE pool_requests ADD COLUMN cache_hit_tokens INTEGER NOT NULL DEFAULT 0"
            )
        if columns and "cache_miss_tokens" not in columns:
            self.conn.execute(
                "ALTER TABLE pool_requests ADD COLUMN cache_miss_tokens INTEGER NOT NULL DEFAULT 0"
            )
        # Ephemeral bindings to providers this platform no longer serves.
        placeholders = ",".join("?" for _ in ROUTABLE_PROVIDER_ORDER)
        self.conn.execute(
            f"DELETE FROM pool_sessions WHERE provider NOT IN ({placeholders})",
            ROUTABLE_PROVIDER_ORDER,
        )

    def close(self) -> None:
        self.conn.close()

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str) -> int:
            row = self.conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        return {
            "accounts": scalar("SELECT COUNT(*) FROM wb_accounts WHERE present = 1"),
            "models": scalar("SELECT COUNT(*) FROM wb_models WHERE present = 1"),
            "pool_keys": scalar("SELECT COUNT(*) FROM pool_keys"),
            "requests": scalar("SELECT COUNT(*) FROM pool_requests"),
            "sessions": scalar("SELECT COUNT(*) FROM pool_sessions"),
        }
