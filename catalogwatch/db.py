from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import ProviderConfig
from .models import Event, ModelRecord
from .util import utcnow, utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    adapter TEXT NOT NULL,
    base_url TEXT NOT NULL,
    key_env TEXT,
    free_kind TEXT NOT NULL DEFAULT 'free_tier',
    enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 30,
    last_polled_at TEXT,
    last_status TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    name TEXT,
    context_length INTEGER,
    max_output INTEGER,
    is_free INTEGER NOT NULL DEFAULT 1,
    free_kind TEXT NOT NULL DEFAULT 'free_tier',
    pricing_json TEXT,
    fingerprint TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    removed_at TEXT,
    PRIMARY KEY (provider_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_models_present ON models(present, is_free);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider_id TEXT,
    subject TEXT,
    summary TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS quota_sources (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    label TEXT NOT NULL,
    url TEXT NOT NULL,
    selector TEXT,
    fields_json TEXT,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_hash TEXT,
    last_checked_at TEXT,
    last_changed_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS quota_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(source_id, content_hash)
);

CREATE TABLE IF NOT EXISTS quota_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    extracted_json TEXT NOT NULL,
    extractor TEXT
);
CREATE INDEX IF NOT EXISTS idx_quota_snapshots_source ON quota_snapshots(source_id, id DESC);
"""


# WorkBuddy pool/control-plane tables. They live alongside the catalog tables and
# are intentionally separate from the bridge's credential database.
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
"""


_UPSERT_MODEL = """
INSERT INTO models (
    provider_id, model_id, name, context_length, max_output, is_free,
    free_kind, pricing_json, fingerprint, first_seen, last_seen, present, removed_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
ON CONFLICT(provider_id, model_id) DO UPDATE SET
    name = excluded.name,
    context_length = excluded.context_length,
    max_output = excluded.max_output,
    is_free = excluded.is_free,
    free_kind = excluded.free_kind,
    pricing_json = excluded.pricing_json,
    fingerprint = excluded.fingerprint,
    last_seen = excluded.last_seen,
    present = 1,
    removed_at = NULL
"""


def _model_values(rec: ModelRecord, now: str) -> tuple[Any, ...]:
    return (
        rec.provider_id,
        rec.model_id,
        rec.name,
        rec.context_length,
        rec.max_output,
        int(rec.is_free),
        rec.free_kind,
        rec.pricing_json(),
        rec.fingerprint(),
        now,
        now,
    )


def _model_event_payload(rec: ModelRecord) -> dict[str, Any]:
    return {
        "name": rec.name,
        "context_length": rec.context_length,
        "max_output": rec.max_output,
        "is_free": rec.is_free,
        "free_kind": rec.free_kind,
        "pricing": rec.pricing,
    }


def _new_summary(provider_name: str, rec: ModelRecord) -> str:
    details: list[str] = []
    if rec.context_length:
        details.append(f"{rec.context_length:,} ctx")
    if rec.is_free:
        details.append("free" if rec.free_kind != "zero_price" else "zero price")
    else:
        details.append("paid")
    return f"{provider_name}: new model {rec.model_id} ({', '.join(details)})"


def _semantic_changed(old: sqlite3.Row, rec: ModelRecord) -> bool:
    """Compare stored values, not fingerprints.

    A fingerprint only answers "is it worth looking closer?". If the fingerprint
    algorithm changes (or the DB was written by an older version), the semantic
    comparison keeps us from emitting an event for every model.
    """
    return (
        old["name"] != rec.name
        or old["context_length"] != rec.context_length
        or old["max_output"] != rec.max_output
        or bool(old["is_free"]) != rec.is_free
        or old["free_kind"] != rec.free_kind
    )


def _changed_summary(provider_name: str, old: sqlite3.Row, rec: ModelRecord) -> str:
    changes: list[str] = []
    if old["context_length"] != rec.context_length:
        changes.append(f"context {old['context_length']}→{rec.context_length}")
    if old["max_output"] != rec.max_output:
        changes.append(f"max output {old['max_output']}→{rec.max_output}")
    if bool(old["is_free"]) != rec.is_free:
        changes.append("became free" if rec.is_free else "no longer free")
    if old["free_kind"] != rec.free_kind:
        changes.append(f"kind {old['free_kind']}→{rec.free_kind}")
    if (old["pricing_json"] or None) != rec.pricing_json():
        changes.append("pricing")
    if old["name"] != rec.name:
        changes.append("name")
    if not changes:
        changes.append("metadata")
    if len(changes) > 3:
        changes = changes[:3] + [f"+{len(changes) - 3} more"]
    return f"{provider_name}: model updated {rec.model_id} ({', '.join(changes)})"


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Serialize the new pool writes with a shared lock. Existing catalog
        # writes remain short and synchronous; WAL + busy_timeout covers them.
        self.write_lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.executescript(WORKBUDDY_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(providers)")}
        if "key_env" not in columns:
            self.conn.execute("ALTER TABLE providers ADD COLUMN key_env TEXT")
        if "free_kind" not in columns:
            self.conn.execute(
                "ALTER TABLE providers ADD COLUMN free_kind TEXT NOT NULL DEFAULT 'free_tier'"
            )
        source_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(quota_sources)")
        }
        if "fields_json" not in source_columns:
            self.conn.execute("ALTER TABLE quota_sources ADD COLUMN fields_json TEXT")
        pool_request_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(pool_requests)")
        }
        if pool_request_columns and "session_id" not in pool_request_columns:
            self.conn.execute("ALTER TABLE pool_requests ADD COLUMN session_id TEXT")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ providers

    def sync_providers(self, providers: Iterable[ProviderConfig]) -> None:
        providers = list(providers)
        now = utcnow_iso()
        ids = [provider.id for provider in providers]
        with self.conn:
            for provider in providers:
                self.conn.execute(
                    """
                    INSERT INTO providers (
                        id, name, adapter, base_url, key_env, free_kind, enabled,
                        poll_interval_minutes, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        adapter = excluded.adapter,
                        base_url = excluded.base_url,
                        key_env = excluded.key_env,
                        free_kind = excluded.free_kind,
                        enabled = excluded.enabled,
                        poll_interval_minutes = excluded.poll_interval_minutes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        provider.id,
                        provider.name,
                        provider.adapter,
                        provider.base_url,
                        provider.key_env,
                        provider.free_kind,
                        int(provider.enabled),
                        provider.poll_interval_minutes,
                        now,
                    ),
                )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE providers SET enabled = 0 WHERE id NOT IN ({placeholders})", ids
                )
            else:
                self.conn.execute("UPDATE providers SET enabled = 0")

    def sync_quota_sources(self, providers: Iterable[ProviderConfig]) -> None:
        providers = list(providers)
        ids: list[str] = []
        with self.conn:
            for provider in providers:
                for source in provider.quota_sources:
                    source_id = f"{provider.id}:{source.label}"
                    ids.append(source_id)
                    self.conn.execute(
                        """
                        INSERT INTO quota_sources (
                            id, provider_id, label, url, selector, fields_json,
                            interval_minutes, enabled
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            provider_id = excluded.provider_id,
                            label = excluded.label,
                            url = excluded.url,
                            selector = excluded.selector,
                            fields_json = excluded.fields_json,
                            interval_minutes = excluded.interval_minutes,
                            enabled = excluded.enabled
                        """,
                        (
                            source_id,
                            provider.id,
                            source.label,
                            source.url,
                            source.selector,
                            json.dumps(source.fields) if source.fields else None,
                            source.interval_minutes,
                            int(provider.enabled and source.enabled),
                        ),
                    )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE quota_sources SET enabled = 0 WHERE id NOT IN ({placeholders})", ids
                )
            else:
                self.conn.execute("UPDATE quota_sources SET enabled = 0")

    def providers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM models m
                     WHERE m.provider_id = p.id AND m.present = 1) AS model_count,
                   (SELECT COUNT(*) FROM models m
                     WHERE m.provider_id = p.id AND m.present = 1 AND m.is_free = 1) AS free_model_count
              FROM providers p
             ORDER BY p.name
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def get_provider(self, provider_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()

    def provider_due(self, provider: ProviderConfig, now: datetime | None = None) -> bool:
        row = self.get_provider(provider.id)
        if row is None or not row["last_polled_at"]:
            return True
        last = datetime.fromisoformat(row["last_polled_at"])
        return last + timedelta(minutes=provider.poll_interval_minutes) <= (now or utcnow())

    def mark_provider_poll(self, provider_id: str, status: str, error: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE providers SET last_polled_at = ?, last_status = ?, last_error = ? WHERE id = ?",
                (utcnow_iso(), status, error, provider_id),
            )

    def present_model_count(self, provider_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM models WHERE provider_id = ? AND present = 1", (provider_id,)
        ).fetchone()
        return int(row[0])

    def has_model_history(self, provider_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM models WHERE provider_id = ? LIMIT 1", (provider_id,)
        ).fetchone()
        return row is not None

    # --------------------------------------------------------------------- models

    def apply_model_snapshot(
        self,
        provider_id: str,
        provider_name: str,
        records: Iterable[ModelRecord],
        baseline: bool = False,
        now: str | None = None,
    ) -> list[Event]:
        """Upsert a provider's model list and return the events it produced."""
        now = now or utcnow_iso()
        current: dict[str, ModelRecord] = {}
        for record in records:
            current[record.model_id] = record

        existing_rows = self.conn.execute(
            "SELECT * FROM models WHERE provider_id = ? AND present = 1", (provider_id,)
        ).fetchall()
        existing = {row["model_id"]: row for row in existing_rows}

        if baseline:
            with self.conn:
                for record in current.values():
                    self.conn.execute(_UPSERT_MODEL, _model_values(record, now))
                self._insert_event(
                    Event(
                        kind="provider.baseline",
                        provider_id=provider_id,
                        subject=provider_id,
                        summary=f"{provider_name}: baseline import, {len(current)} models",
                        payload={"count": len(current)},
                    ),
                    ts=now,
                )
            return [
                Event(
                    kind="provider.baseline",
                    provider_id=provider_id,
                    subject=provider_id,
                    summary=f"{provider_name}: baseline import, {len(current)} models",
                    payload={"count": len(current)},
                )
            ]

        events: list[Event] = []
        with self.conn:
            for model_id, record in current.items():
                old = existing.get(model_id)
                if old is None:
                    self.conn.execute(_UPSERT_MODEL, _model_values(record, now))
                    events.append(
                        Event(
                            kind="model.new",
                            provider_id=provider_id,
                            subject=model_id,
                            summary=_new_summary(provider_name, record),
                            payload=_model_event_payload(record),
                        )
                    )
                elif old["fingerprint"] != record.fingerprint() and _semantic_changed(old, record):
                    self.conn.execute(_UPSERT_MODEL, _model_values(record, now))
                    events.append(
                        Event(
                            kind="model.changed",
                            provider_id=provider_id,
                            subject=model_id,
                            summary=_changed_summary(provider_name, old, record),
                            payload={
                                "before": {
                                    "name": old["name"],
                                    "context_length": old["context_length"],
                                    "max_output": old["max_output"],
                                    "is_free": bool(old["is_free"]),
                                    "free_kind": old["free_kind"],
                                    "pricing": json.loads(old["pricing_json"]) if old["pricing_json"] else None,
                                },
                                "after": _model_event_payload(record),
                            },
                        )
                    )
                else:
                    # Either nothing meaningful changed, or only the price/fingerprint
                    # drifted. Refresh both silently; no event.
                    self.conn.execute(
                        """
                        UPDATE models
                           SET last_seen = ?, pricing_json = ?, fingerprint = ?
                         WHERE provider_id = ? AND model_id = ?
                        """,
                        (
                            now,
                            record.pricing_json(),
                            record.fingerprint(),
                            provider_id,
                            model_id,
                        ),
                    )

            for model_id in existing:
                if model_id in current:
                    continue
                self.conn.execute(
                    """
                    UPDATE models
                       SET present = 0, removed_at = ?, last_seen = ?
                     WHERE provider_id = ? AND model_id = ?
                    """,
                    (now, now, provider_id, model_id),
                )
                events.append(
                    Event(
                        kind="model.removed",
                        provider_id=provider_id,
                        subject=model_id,
                        summary=f"{provider_name}: model removed {model_id}",
                        payload={"model_id": model_id},
                    )
                )

            for event in events:
                self._insert_event(event, ts=now)

        return events

    def models(
        self,
        present: bool | None = True,
        free: bool | None = None,
        provider: str | None = None,
        query: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ["1 = 1"]
        params: list[Any] = []
        if present is not None:
            where.append("m.present = ?")
            params.append(int(present))
        if free is not None:
            where.append("m.is_free = ?")
            params.append(int(free))
        if provider:
            where.append("m.provider_id = ?")
            params.append(provider)
        if query:
            where.append("(m.model_id LIKE ? OR m.name LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        limit = max(1, min(int(limit), 100_000))
        offset = max(0, int(offset))
        rows = self.conn.execute(
            f"""
            SELECT m.*, p.name AS provider_name
              FROM models m
              JOIN providers p ON p.id = m.provider_id
             WHERE {' AND '.join(where)}
             ORDER BY p.name, m.model_id
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        return [self._model_row(row) for row in rows]

    @staticmethod
    def _model_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "provider_id": row["provider_id"],
            "provider_name": row["provider_name"],
            "model_id": row["model_id"],
            "name": row["name"],
            "context_length": row["context_length"],
            "max_output": row["max_output"],
            "is_free": bool(row["is_free"]),
            "free_kind": row["free_kind"],
            "pricing": json.loads(row["pricing_json"]) if row["pricing_json"] else None,
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "present": bool(row["present"]),
            "removed_at": row["removed_at"],
        }

    def export_catalog(self, free_only: bool = True) -> dict[str, Any]:
        rows = self.models(present=True, free=True if free_only else None, limit=100_000)
        meta = {
            row["id"]: dict(row)
            for row in self.conn.execute("SELECT id, base_url, key_env FROM providers").fetchall()
        }
        grouped: dict[str, dict[str, Any]] = {}
        for model in rows:
            provider = grouped.setdefault(
                model["provider_id"],
                {
                    "id": model["provider_id"],
                    "name": model["provider_name"],
                    "base_url": meta.get(model["provider_id"], {}).get("base_url"),
                    "key_env": meta.get(model["provider_id"], {}).get("key_env"),
                    "free_kind": model["free_kind"],
                    "models": [],
                },
            )
            provider["models"].append(
                {
                    key: model[key]
                    for key in (
                        "model_id",
                        "name",
                        "context_length",
                        "max_output",
                        "is_free",
                        "free_kind",
                        "pricing",
                        "first_seen",
                        "last_seen",
                    )
                }
            )
        return {
            "generated_at": utcnow_iso(),
            "free_only": free_only,
            "providers": sorted(grouped.values(), key=lambda item: item["name"].lower()),
        }

    # --------------------------------------------------------------------- events

    def _insert_event(self, event: Event, ts: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO events (ts, kind, provider_id, subject, summary, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ts or utcnow_iso(),
                event.kind,
                event.provider_id,
                event.subject,
                event.summary,
                json.dumps(event.payload, sort_keys=True, default=str) if event.payload else None,
            ),
        )

    def record_event(self, event: Event) -> None:
        with self.conn:
            self._insert_event(event)

    def changes(
        self,
        since: str,
        limit: int = 100,
        kinds: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        sql = """
            SELECT id, ts, kind, provider_id, subject, summary, payload_json
              FROM events
             WHERE ts >= ?
        """
        params: list[Any] = [since]
        kinds = list(kinds or [])
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "kind": row["kind"],
                "provider_id": row["provider_id"],
                "subject": row["subject"],
                "summary": row["summary"],
                "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
            }
            for row in rows
        ]

    # ---------------------------------------------------------------------- quota

    @staticmethod
    def _quota_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        raw = item.pop("fields_json", None)
        try:
            item["fields"] = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            item["fields"] = []
        return item

    def quota_sources(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM quota_sources"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY provider_id, label"
        return [self._quota_dict(row) for row in self.conn.execute(sql).fetchall()]

    def quota_source(self, source_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM quota_sources WHERE id = ?", (source_id,)
        ).fetchone()
        return self._quota_dict(row) if row else None

    @staticmethod
    def quota_source_due(source: dict[str, Any], now: datetime | None = None) -> bool:
        if not source.get("last_checked_at"):
            return True
        last = datetime.fromisoformat(source["last_checked_at"])
        return last + timedelta(minutes=int(source["interval_minutes"])) <= (now or utcnow())

    def mark_quota_checked(
        self,
        source_id: str,
        content_hash: str | None = None,
        changed: bool = False,
        error: str | None = None,
    ) -> None:
        now = utcnow_iso()
        fields = ["last_checked_at = ?"]
        values: list[Any] = [now]
        if content_hash is not None:
            fields.append("last_hash = ?")
            values.append(content_hash)
        if changed:
            fields.append("last_changed_at = ?")
            values.append(now)
        fields.append("last_error = ?")
        values.append(error)
        values.append(source_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE quota_sources SET {', '.join(fields)} WHERE id = ?", values
            )

    def record_quota_doc(
        self, source_id: str, text: str, content_hash: str, fetched_at: str | None = None
    ) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO quota_docs (source_id, fetched_at, content_hash, text)
                VALUES (?, ?, ?, ?)
                """,
                (source_id, fetched_at or utcnow_iso(), content_hash, text),
            )
        return cursor.rowcount > 0

    def latest_quota_text(self, source_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT text FROM quota_docs WHERE source_id = ? ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        return row["text"] if row else None

    def record_quota_snapshot(self, source_id: str, extracted: dict[str, Any], extractor: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO quota_snapshots (source_id, created_at, extracted_json, extractor)
                VALUES (?, ?, ?, ?)
                """,
                (
                    source_id,
                    utcnow_iso(),
                    json.dumps(extracted, sort_keys=True, default=str),
                    extractor,
                ),
            )

    def quota_overview(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT s.*,
                   q.extracted_json, q.created_at AS snapshot_at, q.extractor
              FROM quota_sources s
              LEFT JOIN quota_snapshots q
                ON q.id = (
                    SELECT id FROM quota_snapshots
                     WHERE source_id = s.id
                     ORDER BY id DESC LIMIT 1
                )
             ORDER BY s.provider_id, s.label
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._quota_dict(row)
            raw = item.pop("extracted_json", None)
            item["extracted"] = json.loads(raw) if raw else None
            result.append(item)
        return result

    # ---------------------------------------------------------------------- stats

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str) -> int:
            row = self.conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        return {
            "providers": scalar("SELECT COUNT(*) FROM providers"),
            "providers_enabled": scalar("SELECT COUNT(*) FROM providers WHERE enabled = 1"),
            "models_present": scalar("SELECT COUNT(*) FROM models WHERE present = 1"),
            "models_free_present": scalar(
                "SELECT COUNT(*) FROM models WHERE present = 1 AND is_free = 1"
            ),
            "events": scalar("SELECT COUNT(*) FROM events"),
            "quota_sources": scalar("SELECT COUNT(*) FROM quota_sources WHERE enabled = 1"),
            "last_event_at": (
                self.conn.execute("SELECT ts FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
                if scalar("SELECT COUNT(*) FROM events")
                else None
            ),
        }
