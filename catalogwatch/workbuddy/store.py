"""SQLite access for the WorkBuddy pool control plane.

All methods use the CatalogWatch Database's shared connection and write lock.
Keep transactions short and never call HTTP while holding the lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..db import Database
from ..util import utcnow_iso
from .promotions import parse_credits

_COST_CAPABILITY_KEYS = (
    "supportsToolCall",
    "supportsImages",
    "supportsReasoning",
    "onlyReasoning",
)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class WorkBuddyStore:
    def __init__(self, db: Database):
        self.db = db
        self.conn = db.conn
        self.lock: threading.RLock = db.write_lock

    # ------------------------------------------------------------------ accounts

    def upsert_account(
        self,
        row: dict,
        *,
        available_credits: float | None = None,
        packages: Any = None,
    ) -> int:
        account_id = int(row.get("id") or row.get("account_id"))
        now = utcnow_iso()
        provider = str(row.get("provider") or "workbuddy")
        token_expires_at = row.get("expires_at") or row.get("token_expires_at")
        try:
            token_expires_at = int(token_expires_at) if token_expires_at else None
        except (TypeError, ValueError):
            token_expires_at = None
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO wb_accounts (
                    account_id, provider, name, nickname, status, present,
                    weight, priority, credit_limit, credit_baseline,
                    total_credits, available_credits, packages_json,
                    token_expires_at, last_synced_at, last_seen_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                    provider=excluded.provider,
                    name=excluded.name,
                    nickname=excluded.nickname,
                    status=excluded.status,
                    present=1,
                    weight=excluded.weight,
                    priority=excluded.priority,
                    credit_limit=excluded.credit_limit,
                    credit_baseline=excluded.credit_baseline,
                    total_credits=excluded.total_credits,
                    available_credits=COALESCE(excluded.available_credits, wb_accounts.available_credits),
                    packages_json=COALESCE(excluded.packages_json, wb_accounts.packages_json),
                    token_expires_at=COALESCE(excluded.token_expires_at, wb_accounts.token_expires_at),
                    last_synced_at=excluded.last_synced_at,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    account_id,
                    provider,
                    row.get("name"),
                    row.get("nickname"),
                    str(row.get("status") or "active"),
                    1,
                    int(row.get("weight") or 1),
                    int(row.get("priority") or 0),
                    float(row.get("credit_limit") or 0),
                    float(row.get("credit_baseline") or 0),
                    float(row.get("total_credits") or 0),
                    available_credits,
                    _json(packages),
                    token_expires_at,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO wb_account_state (account_id, updated_at) VALUES (?,?)",
                (account_id, now),
            )
        return account_id

    def mark_missing_accounts(self, present_ids: Iterable[int]) -> None:
        ids = [int(value) for value in present_ids]
        with self.lock, self.conn:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE wb_accounts SET present=0 WHERE account_id NOT IN ({placeholders})",
                    ids,
                )
            else:
                self.conn.execute("UPDATE wb_accounts SET present=0")

    def list_accounts(self, *, present: bool | None = True) -> list[dict]:
        sql = """
            SELECT a.*, s.health, s.cooldown_until, s.free_exhausted_until,
                   s.daily_date, s.daily_requests, s.daily_tokens, s.daily_credits,
                   s.free_requests_today, s.paid_requests_today,
                   s.free_429_count, s.consecutive_failures, s.last_status_code,
                   s.last_error, s.last_429_at,
                   c.expiring_30d AS credit_expiring_30d
              FROM wb_accounts a
              LEFT JOIN wb_account_state s ON s.account_id = a.account_id
              LEFT JOIN credit_snapshots c
                ON c.id = (
                    SELECT id FROM credit_snapshots
                     WHERE account_id = a.account_id
                     ORDER BY id DESC LIMIT 1
                )
        """
        params: list[Any] = []
        if present is not None:
            sql += " WHERE a.present=?"
            params.append(int(present))
        sql += " ORDER BY a.provider, a.priority DESC, a.account_id"
        rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["packages"] = _loads(item.pop("packages_json", None), [])
            result.append(item)
        return result

    def get_account(self, account_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM wb_accounts WHERE account_id=?", (int(account_id),)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["packages"] = _loads(item.pop("packages_json", None), [])
        return item

    def mark_account_missing(self, account_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE wb_accounts SET present=0 WHERE account_id=?",
                (int(account_id),),
            )

    # --------------------------------------------------------------------- state

    def get_state(self, account_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM wb_account_state WHERE account_id=?", (int(account_id),)
        ).fetchone()
        if row is None:
            now = utcnow_iso()
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO wb_account_state (account_id, updated_at) VALUES (?,?)",
                    (int(account_id), now),
                )
            row = self.conn.execute(
                "SELECT * FROM wb_account_state WHERE account_id=?", (int(account_id),)
            ).fetchone()
        return dict(row) if row else {}

    def reset_daily_if_needed(self, account_id: int, day: str) -> None:
        state = self.get_state(account_id)
        if state.get("daily_date") == day:
            return
        with self.lock, self.conn:
            self.conn.execute(
                """
                UPDATE wb_account_state SET
                    daily_date=?, daily_requests=0, daily_tokens=0, daily_credits=0,
                    free_requests_today=0, paid_requests_today=0,
                    free_429_count=0, updated_at=?
                WHERE account_id=?
                """,
                (day, utcnow_iso(), int(account_id)),
            )

    def update_state(self, account_id: int, **fields: Any) -> None:
        allowed = {
            "health",
            "cooldown_until",
            "free_exhausted_until",
            "daily_date",
            "daily_requests",
            "daily_tokens",
            "daily_credits",
            "free_requests_today",
            "paid_requests_today",
            "free_429_count",
            "consecutive_failures",
            "last_status_code",
            "last_error",
            "last_429_at",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return
        clean["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{key}=?" for key in clean)
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE wb_account_state SET {assignments} WHERE account_id=?",
                [*clean.values(), int(account_id)],
            )

    def note_success(self, account_id: int) -> None:
        self.update_state(
            account_id,
            health="ok",
            consecutive_failures=0,
            last_status_code=200,
            last_error=None,
            cooldown_until=None,
        )

    def note_failure(
        self,
        account_id: int,
        status_code: int | None,
        error: str | None,
        *,
        cooldown_until: str | None = None,
    ) -> None:
        state = self.get_state(account_id)
        failures = int(state.get("consecutive_failures") or 0) + 1
        self.update_state(
            account_id,
            health="error",
            consecutive_failures=failures,
            last_status_code=status_code,
            last_error=(error or "")[:500],
            cooldown_until=cooldown_until,
        )

    def set_cooldown(self, account_id: int, until: str | None, reason: str | None = None) -> None:
        self.update_state(account_id, cooldown_until=until, last_error=(reason or "")[:500])

    def set_free_exhausted(self, account_id: int, until: str | None) -> None:
        self.update_state(account_id, free_exhausted_until=until, last_429_at=utcnow_iso())

    def increment_daily(self, account_id: int, *, free: bool, tokens: int, credits: float) -> None:
        day = utcnow_iso()[:10]
        self.reset_daily_if_needed(account_id, day)
        column = "free_requests_today" if free else "paid_requests_today"
        with self.lock, self.conn:
            self.conn.execute(
                f"""
                UPDATE wb_account_state SET
                    daily_requests=daily_requests + 1,
                    daily_tokens=daily_tokens + ?,
                    daily_credits=daily_credits + ?,
                    {column}={column} + 1,
                    updated_at=?
                WHERE account_id=?
                """,
                (int(tokens), float(credits or 0), utcnow_iso(), int(account_id)),
            )

    # ---------------------------------------------------------------------- caps

    def get_caps(self, account_id: int, model_id: str) -> dict:
        rows = self.conn.execute(
            """
            SELECT * FROM wb_account_caps
             WHERE account_id=? AND model_id IN (?, '*')
             ORDER BY CASE WHEN model_id='*' THEN 1 ELSE 0 END
            """,
            (int(account_id), str(model_id)),
        ).fetchall()
        result = {"daily_request_cap": 0, "daily_credit_cap": 0.0}
        for row in rows:
            if int(row["daily_request_cap"] or 0):
                result["daily_request_cap"] = int(row["daily_request_cap"])
            if float(row["daily_credit_cap"] or 0):
                result["daily_credit_cap"] = float(row["daily_credit_cap"])
        return result

    def set_cap(
        self,
        account_id: int,
        model_id: str,
        *,
        daily_request_cap: int = 0,
        daily_credit_cap: float = 0,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO wb_account_caps
                    (account_id, model_id, daily_request_cap, daily_credit_cap)
                VALUES (?,?,?,?)
                ON CONFLICT(account_id, model_id) DO UPDATE SET
                    daily_request_cap=excluded.daily_request_cap,
                    daily_credit_cap=excluded.daily_credit_cap
                """,
                (int(account_id), str(model_id), max(0, int(daily_request_cap)), max(0.0, float(daily_credit_cap))),
            )

    # -------------------------------------------------------------------- models

    def upsert_models(
        self, provider: str, models: list[dict], *, source_identity: str | None = None
    ) -> int:
        now = utcnow_iso()
        seen: list[str] = []
        with self.lock, self.conn:
            for item in models:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or "").strip()
                if not model_id:
                    continue
                seen.append(model_id)
                capabilities = {
                    key: item[key]
                    for key in _COST_CAPABILITY_KEYS
                    if key in item and item[key] is not None
                }
                context_length = (
                    item.get("context_length")
                    or item.get("context_window")
                    or item.get("maxInputTokens")
                    or item.get("max_input_tokens")
                )
                max_output = (
                    item.get("max_output")
                    or item.get("max_output_tokens")
                    or item.get("maxOutputTokens")
                )
                base = parse_credits(item.get("credits"))
                raw = item.get("credits_raw", item.get("credits"))
                self.conn.execute(
                    """
                    INSERT INTO wb_models (
                        provider, model_id, name, context_length, max_output,
                        base_credits, credits_raw, capabilities_json, tags_json,
                        official, present, source_identity, first_seen, last_seen, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider, model_id) DO UPDATE SET
                        name=excluded.name,
                        context_length=COALESCE(excluded.context_length, wb_models.context_length),
                        max_output=COALESCE(excluded.max_output, wb_models.max_output),
                        base_credits=excluded.base_credits,
                        credits_raw=excluded.credits_raw,
                        capabilities_json=excluded.capabilities_json,
                        tags_json=excluded.tags_json,
                        official=excluded.official,
                        present=1,
                        source_identity=excluded.source_identity,
                        last_seen=excluded.last_seen,
                        updated_at=excluded.updated_at
                    """,
                    (
                        provider,
                        model_id,
                        item.get("name") or model_id,
                        int(context_length) if context_length else None,
                        int(max_output) if max_output else None,
                        base,
                        str(raw) if raw is not None else None,
                        _json(capabilities),
                        _json(item.get("tags")),
                        1 if item.get("official") else 0,
                        1,
                        source_identity,
                        now,
                        now,
                        now,
                    ),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                self.conn.execute(
                    f"UPDATE wb_models SET present=0 WHERE provider=? AND model_id NOT IN ({placeholders})",
                    [provider, *seen],
                )
            else:
                self.conn.execute("UPDATE wb_models SET present=0 WHERE provider=?", (provider,))
        return len(seen)

    def list_models(self, provider: str | None = None, *, present: bool = True) -> list[dict]:
        sql = "SELECT * FROM wb_models"
        where = []
        params: list[Any] = []
        if provider:
            where.append("provider=?")
            params.append(provider)
        if present:
            where.append("present=1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY provider, model_id"
        rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["capabilities"] = _loads(item.pop("capabilities_json", None), {})
            item["tags"] = _loads(item.pop("tags_json", None), [])
            result.append(item)
        return result

    def get_model(self, provider: str, model_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM wb_models WHERE provider=? AND model_id=?",
            (provider, model_id),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["capabilities"] = _loads(item.pop("capabilities_json", None), {})
        item["tags"] = _loads(item.pop("tags_json", None), [])
        return item

    # ---------------------------------------------------------------- promotions

    def replace_promotions(
        self,
        provider: str,
        grouped: dict[str, list[dict]],
        *,
        source_identity: str | None = None,
    ) -> int:
        now = utcnow_iso()
        inserted = 0
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM wb_promotions WHERE provider=?", (provider,))
            seen: set[tuple] = set()
            for model_id, rules in grouped.items():
                for rule in rules:
                    label = rule.get("label")
                    factor = rule.get("factor")
                    schedule = rule.get("schedule")
                    key = (model_id, label, factor, _json(schedule))
                    if key in seen:
                        continue
                    seen.add(key)
                    self.conn.execute(
                        """
                        INSERT INTO wb_promotions (
                            provider, model_id, label, color, factor, priority,
                            active_only, schedule_json, text_zh, text_en,
                            source_identity, fetched_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            provider,
                            model_id,
                            label,
                            rule.get("color"),
                            factor,
                            int(rule.get("priority") or 0),
                            1 if rule.get("active_only") else 0,
                            _json(schedule),
                            rule.get("text_zh") or "",
                            rule.get("text_en") or "",
                            source_identity,
                            now,
                        ),
                    )
                    inserted += 1
        return inserted

    def get_promotions(self, provider: str, model_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM wb_promotions WHERE provider=? AND model_id=?",
            (provider, model_id),
        ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "label": row["label"],
                    "color": row["color"],
                    "factor": row["factor"],
                    "priority": int(row["priority"] or 0),
                    "active_only": bool(row["active_only"]),
                    "schedule": _loads(row["schedule_json"], None),
                    "text_zh": row["text_zh"] or "",
                    "text_en": row["text_en"] or "",
                }
            )
        return result

    def get_manual_cost(self, provider: str, model_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM manual_model_costs WHERE provider=? AND model_id=? AND enabled=1",
            (provider, model_id),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def upsert_manual_cost(
        self,
        provider: str,
        model_id: str,
        *,
        base_credits: float | None,
        factor: float | None,
        daily_windows: list[dict] | None,
        timezone_name: str,
        valid_from: str | None,
        valid_until: str | None,
        enabled: bool = True,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO manual_model_costs (
                    provider, model_id, base_credits, factor, daily_windows_json,
                    timezone, valid_from, valid_until, enabled, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider, model_id) DO UPDATE SET
                    base_credits=excluded.base_credits,
                    factor=excluded.factor,
                    daily_windows_json=excluded.daily_windows_json,
                    timezone=excluded.timezone,
                    valid_from=excluded.valid_from,
                    valid_until=excluded.valid_until,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    provider,
                    model_id,
                    base_credits,
                    factor,
                    _json(daily_windows or []),
                    timezone_name,
                    valid_from,
                    valid_until,
                    1 if enabled else 0,
                    utcnow_iso(),
                ),
            )

    def delete_manual_cost(self, provider: str, model_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM manual_model_costs WHERE provider=? AND model_id=?",
                (provider, model_id),
            )

    def record_config_snapshot(
        self,
        *,
        provider: str,
        account_id: int | None,
        identity: str | None,
        status: str,
        model_count: int,
        promotion_count: int,
        payload_hash: str | None,
        payload_json: str | None,
        warning: str | None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO wb_config_snapshots (
                    provider, account_id, identity, fetched_at, status,
                    model_count, promotion_count, payload_hash, payload_json, warning
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider,
                    account_id,
                    identity,
                    utcnow_iso(),
                    status,
                    model_count,
                    promotion_count,
                    payload_hash,
                    payload_json,
                    warning,
                ),
            )

    # -------------------------------------------------------------------- credits

    def record_credit_snapshot(self, account_id: int, payload: dict) -> None:
        now = utcnow_iso()
        packages = payload.get("packages")
        ok = bool(payload.get("ok", True))
        error = None if ok else str(payload.get("message") or payload.get("error") or "")[:500]
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO credit_snapshots (
                    account_id, ts, available_total, total_dosage,
                    expiring_7d, expiring_30d, package_count, packages_json,
                    ok, error
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(account_id),
                    now,
                    payload.get("available_total"),
                    payload.get("total_dosage"),
                    payload.get("expiring_7d_total"),
                    payload.get("expiring_30d_total"),
                    payload.get("package_count"),
                    _json(packages),
                    1 if ok else 0,
                    error,
                ),
            )
            if ok:
                self.conn.execute(
                    """
                    UPDATE wb_accounts SET
                        available_credits=COALESCE(?, available_credits),
                        total_credits=COALESCE(?, total_credits),
                        packages_json=COALESCE(?, packages_json),
                        last_synced_at=?,
                        last_seen_at=?
                    WHERE account_id=?
                    """,
                    (
                        payload.get("available_total"),
                        payload.get("total_dosage"),
                        _json(packages),
                        now,
                        now,
                        int(account_id),
                    ),
                )

    def latest_credit(self, account_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM credit_snapshots WHERE account_id=? ORDER BY id DESC LIMIT 1",
            (int(account_id),),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["packages"] = _loads(item.pop("packages_json", None), [])
        return item

    # ------------------------------------------------------------------- settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value_json FROM pool_settings WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        return _loads(row["value_json"], default)

    def set_setting(self, key: str, value: Any) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO pool_settings (key, value_json, updated_at)
                VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, _json(value), utcnow_iso()),
            )

    def audit(self, actor: str, action: str, target: str | None = None, payload: Any = None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, payload_json) VALUES (?,?,?,?,?)",
                (utcnow_iso(), actor, action, target, _json(payload)),
            )

    def list_audit(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(1000, limit)),)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", None), None)
            result.append(item)
        return result

    # ------------------------------------------------------------------ pool keys

    def create_pool_key(
        self,
        *,
        name: str,
        key_hash: str,
        key_prefix: str,
        kind: str,
        allowed_models: list[str] | None,
        daily_request_limit: int,
        daily_credit_limit: float,
        max_attempts: int,
    ) -> int:
        with self.lock, self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO pool_keys (
                    name, key_prefix, key_hash, status, kind,
                    allowed_models_json, daily_request_limit, daily_credit_limit,
                    max_attempts, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    name,
                    key_prefix,
                    key_hash,
                    "active",
                    kind,
                    _json(allowed_models) if allowed_models else None,
                    max(0, int(daily_request_limit)),
                    max(0.0, float(daily_credit_limit)),
                    max(1, int(max_attempts)),
                    utcnow_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def list_pool_keys(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pool_keys ORDER BY id DESC"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("key_hash", None)
            item["allowed_models"] = _loads(item.pop("allowed_models_json", None), None)
            result.append(item)
        return result

    def get_pool_key_by_hash(self, key_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pool_keys WHERE key_hash=? AND status='active'", (key_hash,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["allowed_models"] = _loads(item.get("allowed_models_json"), None)
        return item

    def get_pool_key(self, key_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pool_keys WHERE id=?", (int(key_id),)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item.pop("key_hash", None)
        item["allowed_models"] = _loads(item.get("allowed_models_json"), None)
        return item

    def latest_config_snapshot(self, provider: str | None = None) -> dict | None:
        if provider:
            row = self.conn.execute(
                "SELECT * FROM wb_config_snapshots WHERE provider=? ORDER BY id DESC LIMIT 1",
                (provider,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM wb_config_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item.pop("payload_json", None)
        return item

    def update_pool_key(self, key_id: int, fields: dict) -> None:
        allowed = {
            "name",
            "status",
            "allowed_models_json",
            "daily_request_limit",
            "daily_credit_limit",
            "max_attempts",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return
        assignments = ", ".join(f"{key}=?" for key in clean)
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE pool_keys SET {assignments} WHERE id=?",
                [*clean.values(), int(key_id)],
            )

    def delete_pool_key(self, key_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM pool_keys WHERE id=?", (int(key_id),))

    def reserve_key_request(self, key_id: int, day: str, limit: int) -> bool:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO pool_key_daily_usage (pool_key_id, usage_date)
                VALUES (?,?)
                """,
                (int(key_id), day),
            )
            row = self.conn.execute(
                "SELECT requests FROM pool_key_daily_usage WHERE pool_key_id=? AND usage_date=?",
                (int(key_id), day),
            ).fetchone()
            used = int(row["requests"] or 0) if row else 0
            if limit and used >= limit:
                return False
            self.conn.execute(
                """
                UPDATE pool_key_daily_usage SET requests=requests + 1
                 WHERE pool_key_id=? AND usage_date=?
                """,
                (int(key_id), day),
            )
            return True

    def add_key_usage(self, key_id: int, day: str, *, tokens: int, credits: float) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO pool_key_daily_usage (pool_key_id, usage_date, requests, tokens, credits)
                VALUES (?,?,0,?,?)
                ON CONFLICT(pool_key_id, usage_date) DO UPDATE SET
                    tokens=tokens + excluded.tokens,
                    credits=credits + excluded.credits
                """,
                (int(key_id), day, int(tokens), float(credits or 0)),
            )
            self.conn.execute(
                """
                UPDATE pool_keys SET
                    last_used_at=?,
                    total_requests=total_requests + 1,
                    total_tokens=total_tokens + ?,
                    total_credits=total_credits + ?
                WHERE id=?
                """,
                (utcnow_iso(), int(tokens), float(credits or 0), int(key_id)),
            )

    def key_usage_today(self, key_id: int, day: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM pool_key_daily_usage WHERE pool_key_id=? AND usage_date=?",
            (int(key_id), day),
        ).fetchone()
        return dict(row) if row else {"requests": 0, "tokens": 0, "credits": 0}

    # ------------------------------------------------------------------- sessions

    def get_session(self, session_id: str) -> dict | None:
        if not session_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM pool_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        now = utcnow_iso()
        if str(row["expires_at"] or "") <= now:
            self.delete_session(session_id)
            return None
        return dict(row)

    def upsert_session(
        self,
        session_id: str,
        *,
        provider: str,
        model_id: str,
        account_id: int | None,
        ttl_seconds: int = 1800,
    ) -> None:
        if not session_id:
            return
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=max(60, int(ttl_seconds)))
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO pool_sessions (
                    session_id, provider, model_id, account_id,
                    created_at, last_used_at, expires_at, request_count
                ) VALUES (?,?,?,?,?,?,?,1)
                ON CONFLICT(session_id) DO UPDATE SET
                    provider=excluded.provider,
                    model_id=excluded.model_id,
                    account_id=excluded.account_id,
                    last_used_at=excluded.last_used_at,
                    expires_at=excluded.expires_at,
                    request_count=pool_sessions.request_count + 1
                """,
                (
                    session_id,
                    provider,
                    model_id,
                    int(account_id) if account_id is not None else None,
                    now.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                    expires.isoformat(timespec="seconds"),
                ),
            )

    def delete_session(self, session_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM pool_sessions WHERE session_id=?", (session_id,)
            )

    def cleanup_pool_sessions(self, before: str | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM pool_sessions WHERE expires_at <= ?",
                (before or utcnow_iso(),),
            )

    def list_sessions(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pool_sessions ORDER BY last_used_at DESC LIMIT ?",
            (max(1, min(500, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- pool request

    def start_request(
        self,
        *,
        request_id: str,
        pool_key_id: int | None,
        endpoint: str,
        model_requested: str,
        stream: bool,
        intent: Any,
        routing: Any,
        session_id: str | None = None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO pool_requests (
                    request_id, session_id, pool_key_id, endpoint, model_requested,
                    intent_json, stream, routing_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    session_id,
                    pool_key_id,
                    endpoint,
                    model_requested,
                    _json(intent),
                    1 if stream else 0,
                    _json(routing),
                    utcnow_iso(),
                ),
            )

    def record_attempt(
        self,
        *,
        request_id: str,
        attempt_no: int,
        account_id: int | None,
        model_id: str | None,
        provider: str | None,
        status_code: int | None,
        error_code: str | None,
        started_at: str,
        duration_ms: int,
        credits: float | None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO pool_request_attempts (
                    request_id, attempt_no, account_id, model_id, provider,
                    status_code, error_code, started_at, duration_ms, credits
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    int(attempt_no),
                    account_id,
                    model_id,
                    provider,
                    status_code,
                    error_code,
                    started_at,
                    int(duration_ms),
                    credits,
                ),
            )

    def finish_request(self, request_id: str, **fields: Any) -> None:
        allowed = {
            "model_routed",
            "provider",
            "bridge_account_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "credits",
            "credits_estimated",
            "effective_credits",
            "cost_source",
            "status_code",
            "finish_reason",
            "error_code",
            "attempts",
            "duration_ms",
            "reconciled_at",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return
        assignments = ", ".join(f"{key}=?" for key in clean)
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE pool_requests SET {assignments} WHERE request_id=?",
                [*clean.values(), request_id],
            )

    def get_request(self, request_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pool_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["intent"] = _loads(item.pop("intent_json", None), None)
        item["routing"] = _loads(item.pop("routing_json", None), None)
        return item

    def list_attempts(self, request_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pool_request_attempts WHERE request_id=? ORDER BY attempt_no",
            (request_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_requests(self, *, limit: int = 100, offset: int = 0, filters: dict | None = None) -> list[dict]:
        filters = filters or {}
        where = ["1=1"]
        params: list[Any] = []
        for key in ("pool_key_id", "bridge_account_id", "status_code"):
            value = filters.get(key)
            if value not in (None, "", "all"):
                where.append(f"{key}=?")
                params.append(int(value))
        model = filters.get("model")
        if model:
            where.append("(model_routed=? OR model_requested=?)")
            params.extend([model, model])
        sql = (
            "SELECT * FROM pool_requests WHERE "
            + " AND ".join(where)
            + " ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, min(1000, int(limit))), max(0, int(offset))])
        rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["intent"] = _loads(item.pop("intent_json", None), None)
            item["routing"] = _loads(item.pop("routing_json", None), None)
            result.append(item)
        return result

    def account_model_usage_today(self, account_id: int, model_id: str, day: str) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(credits), 0) AS credits
              FROM pool_requests
             WHERE bridge_account_id=? AND model_routed=?
               AND created_at LIKE ?
               AND status_code BETWEEN 200 AND 299
            """,
            (int(account_id), model_id, f"{day}%"),
        ).fetchone()
        return dict(row) if row else {"requests": 0, "tokens": 0, "credits": 0}

    def account_usage_today(self, account_id: int, day: str) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(credits), 0) AS credits
              FROM pool_requests
             WHERE bridge_account_id=?
               AND created_at LIKE ?
               AND status_code BETWEEN 200 AND 299
            """,
            (int(account_id), f"{day}%"),
        ).fetchone()
        return dict(row) if row else {"requests": 0, "tokens": 0, "credits": 0}

    def pending_requests(
        self, limit: int = 50, *, older_than_seconds: int = 30
    ) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(0, older_than_seconds))
        ).isoformat(timespec="seconds")
        rows = self.conn.execute(
            """
            SELECT * FROM pool_requests
             WHERE cost_source='pending' AND created_at <= ?
             ORDER BY id LIMIT ?
            """,
            (cutoff, max(1, min(200, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_request(
        self,
        request_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        credits: float,
        status_code: int,
        finish_reason: str | None,
    ) -> bool:
        """Fill exact usage from the bridge log and adjust dependent counters."""
        now = utcnow_iso()
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM pool_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return False
            old_tokens = int(row["total_tokens"] or 0)
            old_credits = float(row["credits"] or 0)
            new_tokens = int(total_tokens)
            new_credits = float(credits or 0)
            delta_tokens = new_tokens - old_tokens
            delta_credits = new_credits - old_credits
            self.conn.execute(
                """
                UPDATE pool_requests SET
                    prompt_tokens=?, completion_tokens=?, total_tokens=?,
                    credits=?, credits_estimated=0, cost_source='reconciled',
                    status_code=?, finish_reason=?, reconciled_at=?
                WHERE request_id=?
                """,
                (
                    int(prompt_tokens),
                    int(completion_tokens),
                    new_tokens,
                    new_credits,
                    int(status_code),
                    finish_reason,
                    now,
                    request_id,
                ),
            )
            key_id = row["pool_key_id"]
            if key_id:
                day = str(row["created_at"] or now)[:10]
                self.conn.execute(
                    """
                    UPDATE pool_key_daily_usage SET
                        tokens=MAX(0, tokens + ?),
                        credits=MAX(0, credits + ?)
                    WHERE pool_key_id=? AND usage_date=?
                    """,
                    (delta_tokens, delta_credits, int(key_id), day),
                )
                self.conn.execute(
                    """
                    UPDATE pool_keys SET
                        total_tokens=MAX(0, total_tokens + ?),
                        total_credits=MAX(0, total_credits + ?)
                    WHERE id=?
                    """,
                    (delta_tokens, delta_credits, int(key_id)),
                )
            account_id = row["bridge_account_id"]
            if account_id:
                self.conn.execute(
                    """
                    UPDATE wb_account_state SET
                        daily_tokens=MAX(0, daily_tokens + ?),
                        daily_credits=MAX(0, daily_credits + ?),
                        updated_at=?
                    WHERE account_id=?
                    """,
                    (delta_tokens, delta_credits, now, int(account_id)),
                )
            attempt_no = int(row["attempts"] or 1)
            self.conn.execute(
                """
                UPDATE pool_request_attempts SET credits=?, status_code=?
                WHERE request_id=? AND attempt_no=?
                """,
                (new_credits, int(status_code), request_id, attempt_no),
            )
        return True

    def mark_reconcile_unknown(self, request_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                UPDATE pool_requests SET cost_source='unknown', reconciled_at=?
                WHERE request_id=? AND cost_source='pending'
                """,
                (utcnow_iso(), request_id),
            )

    # ------------------------------------------------------------------- sessions

    def create_admin_session(
        self, id_hash: str, expires_at: str, user_agent: str | None
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO admin_sessions (id_hash, created_at, expires_at, last_seen_at, user_agent) VALUES (?,?,?,?,?)",
                (id_hash, utcnow_iso(), expires_at, utcnow_iso(), user_agent),
            )

    def get_admin_session(self, id_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM admin_sessions WHERE id_hash=?", (id_hash,)
        ).fetchone()
        return dict(row) if row else None

    def touch_admin_session(self, id_hash: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE admin_sessions SET last_seen_at=? WHERE id_hash=?",
                (utcnow_iso(), id_hash),
            )

    def delete_admin_session(self, id_hash: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM admin_sessions WHERE id_hash=?", (id_hash,))

    def cleanup_admin_sessions(self, before: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (before,))
