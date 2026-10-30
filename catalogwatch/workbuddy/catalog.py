"""Near-real-time WorkBuddy model/cost catalog.

`/v1/models` is the served list; `/admin/workbuddy/config` enriches it with
`credits`, capabilities, and `modelPromotions`. Unknown cost metadata is kept
unknown; the scheduler must never treat a missing multiplier as free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

from ..util import utcnow_iso
from .bridge import BridgeError, BuddyBridge
from .model_filter import is_text_chat_model
from .promotions import (
    DEFAULT_TIMEZONE,
    apply_promotion,
    group_promotions,
    manual_rule,
    parse_credits,
)
from .store import WorkBuddyStore, _loads

log = logging.getLogger(__name__)


class WorkBuddyCatalog:
    def __init__(
        self,
        store: WorkBuddyStore,
        bridge: BuddyBridge,
        providers: tuple[str, ...] = ("workbuddy", "workbuddy-i"),
    ):
        self.store = store
        self.bridge = bridge
        self.providers = providers

    def identity(self) -> str:
        value = self.store.get_setting("config_identity", None)
        if not value:
            value = os.environ.get("CB_WORKBUDDY_CONFIG_IDENTITY", "current")
        return str(value or "current").strip().lower()

    async def refresh(self, *, force: bool = False) -> list[dict]:
        identity = self.identity()
        # Refresh the bridge's stored catalog first; /v1/models reads that store.
        try:
            await self.bridge.refresh_models()
        except BridgeError as exc:
            log.warning("bridge model refresh failed: %s", exc)
        # A realm with no logged-in account has no config to read; probing it
        # returns 409 no_account and used to surface as a permanent console
        # warning. Skip the probe and record no warning for those realms.
        with_accounts = {
            str(account.get("provider") or "")
            for account in self.store.list_accounts(present=True)
        }
        results: list[dict] = []
        for provider in self.providers:
            results.append(
                await self._refresh_provider(
                    provider,
                    identity,
                    force=force,
                    has_account=provider in with_accounts,
                )
            )
        return results

    async def _served_models(self, provider: str) -> list[dict]:
        payload = await self.bridge.models()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        merged: dict[str, dict] = {}
        prefix = f"{provider}/"
        for entry in data:
            if not isinstance(entry, dict):
                continue
            channel = str(entry.get("channel") or "")
            if channel != provider:
                continue
            raw_id = str(entry.get("id") or "").strip()
            if not raw_id:
                continue
            model_id = raw_id[len(prefix) :] if raw_id.startswith(prefix) else raw_id
            if not model_id:
                continue
            current = merged.setdefault(model_id, {})
            # Merge duplicate bare/namespaced entries; keep non-empty fields.
            for key, value in entry.items():
                if value is not None and key not in current:
                    current[key] = value
            current["id"] = model_id
        return list(merged.values())

    async def _refresh_provider(
        self, provider: str, identity: str, *, force: bool, has_account: bool = True
    ) -> dict:
        served: list[dict] = []
        served_error = None
        try:
            served = await self._served_models(provider)
        except BridgeError as exc:
            served_error = f"served list: HTTP {exc.status_code}: {exc}"[:300]

        # No logged-in account for this realm means there is no config to read
        # (the bridge answers 409 no_account). Skip it quietly instead of
        # surfacing a permanent warning in the console.
        if not has_account and not served:
            return {
                "provider": provider,
                "ok": True,
                "models": 0,
                "filtered": 0,
                "promotions": 0,
                "warning": None,
                "skipped": "no_account",
                "served_error": served_error,
                "config_error": None,
            }

        config: dict | None = None
        config_error = None
        if has_account:
            try:
                config = await self.bridge.workbuddy_config(
                    provider, identity=identity, force=force
                )
            except BridgeError as exc:
                config_error = f"config: HTTP {exc.status_code}: {exc}"[:300]

        if served:
            by_id = {}
            if config:
                for item in config.get("models") or []:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        by_id[item["id"]] = item
            merged = []
            for entry in served:
                item = dict(entry)
                extra = by_id.get(item["id"])
                if extra:
                    for key, value in extra.items():
                        if value is not None and key not in item:
                            item[key] = value
                    # Cost/capability metadata from the config wins over /v1/models.
                    for key in (
                        "credits",
                        "credits_raw",
                        "supportsToolCall",
                        "supportsImages",
                        "supportsReasoning",
                        "onlyReasoning",
                        "contextWindow",
                        "official",
                        "tags",
                    ):
                        if key in extra:
                            item[key] = extra[key]
                merged.append(item)
            # 池只服务文本对话：先把伪模型（auto/default）和视频/音频/图像等
            # 非文本模型剔掉。upsert_models 会把不在列表里的旧行标成 present=0，
            # 所以之前展示过的媒体模型下一次刷新就消失。
            served_count = len(merged)
            merged = [
                item
                for item in merged
                if is_text_chat_model(item.get("id"), item.get("name"))
            ]
            filtered_out = served_count - len(merged)
            self.store.upsert_models(provider, merged, source_identity=identity)

            if config and config.get("promotions_available"):
                grouped = group_promotions(config.get("modelPromotions"))
                self.store.replace_promotions(
                    provider, grouped, source_identity=identity
                )
                promotion_count = sum(len(rules) for rules in grouped.values())
                warning = None
            else:
                promotion_count = 0
                if not has_account:
                    warning = None
                else:
                    warning = "promotions_unavailable" if config else (config_error or "config_failed")
            self.store.record_config_snapshot(
                provider=provider,
                account_id=(config or {}).get("account_id"),
                identity=(config or {}).get("identity_used") or identity,
                status="ok" if config else "partial",
                model_count=len(merged),
                promotion_count=promotion_count,
                payload_hash=_payload_hash(config),
                payload_json=_payload_json(config),
                warning=warning,
            )
            return {
                "provider": provider,
                "ok": True,
                "models": len(merged),
                "filtered": filtered_out,
                "promotions": promotion_count,
                "warning": warning,
                "served_error": served_error,
                "config_error": config_error,
            }

        # Never wipe an existing catalog because a fetch failed.
        warning = served_error or config_error or "no_models"
        self.store.record_config_snapshot(
            provider=provider,
            account_id=(config or {}).get("account_id"),
            identity=(config or {}).get("identity_used") or identity,
            status="error",
            model_count=0,
            promotion_count=0,
            payload_hash=_payload_hash(config),
            payload_json=_payload_json(config),
            warning=warning,
        )
        return {
            "provider": provider,
            "ok": False,
            "models": 0,
            "promotions": 0,
            "warning": warning,
            "served_error": served_error,
            "config_error": config_error,
        }

    # ------------------------------------------------------------------ cost view

    def effective_cost(
        self, provider: str, model_id: str, now: datetime | None = None
    ) -> dict | None:
        model = self.store.get_model(provider, model_id)
        if model is None:
            return None
        # 防御：旧行或手动成本里可能还有伪模型/媒体模型。即使上游还没刷新，
        # 也不让它进列表、更不让会话绑到它上面。
        if not is_text_chat_model(model.get("model_id"), model.get("name")):
            return None
        base = parse_credits(model.get("base_credits"))
        promotions = self.store.get_promotions(provider, model_id)
        source = (
            "upstream"
            if model.get("base_credits") is not None or promotions
            else "unknown"
        )

        manual = self.store.get_manual_cost(provider, model_id)
        if manual:
            if manual.get("base_credits") is not None:
                base = parse_credits(manual["base_credits"])
            factor = manual.get("factor")
            windows = _loads(manual.get("daily_windows_json"), []) or []
            rule = manual_rule(
                factor=float(factor) if factor is not None else None,
                daily_windows=windows,
                timezone_name=manual.get("timezone") or DEFAULT_TIMEZONE,
                valid_from=manual.get("valid_from"),
                valid_until=manual.get("valid_until"),
            )
            promotions = [rule, *promotions]
            if manual.get("base_credits") is not None or factor is not None:
                source = "manual"

        applied = apply_promotion(base, promotions, now)
        return {
            "provider": provider,
            "model_id": model_id,
            "name": model.get("name"),
            "context_length": model.get("context_length"),
            "max_output": model.get("max_output"),
            "capabilities": model.get("capabilities") or {},
            "base_credits": base,
            "credits": applied["credits"],
            "free": applied["free"],
            "badge": applied["badge"],
            "badge_color": applied["badge_color"],
            "promotion_active": applied["promotion_active"],
            "promotion_factor": applied["promotion_factor"],
            "promotion_until": applied["promotion_until"],
            "promotion_text_zh": applied["promotion_text_zh"],
            "promotion_text_en": applied["promotion_text_en"],
            "cost_source": source,
            "promotions": promotions,
            "present": bool(model.get("present", 1)),
        }

    def list_costs(
        self, provider: str | None = None, now: datetime | None = None
    ) -> list[dict]:
        result = []
        for model in self.store.list_models(provider):
            cost = self.effective_cost(
                model["provider"], model["model_id"], now=now
            )
            if cost is not None:
                result.append(cost)
        return result


def _payload_json(config: dict | None) -> str | None:
    if not config:
        return None
    try:
        text = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return text[:200_000]


def _payload_hash(config: dict | None) -> str | None:
    text = _payload_json(config)
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cost_summary(costs: list[dict]) -> dict[str, Any]:
    free = [item for item in costs if item.get("free")]
    known = [item for item in costs if item.get("credits") is not None]
    return {
        "total": len(costs),
        "free": len(free),
        "known_cost": len(known),
        "unknown_cost": len(costs) - len(known),
        "min_credits": min((item["credits"] for item in known), default=None),
        "updated_at": utcnow_iso(),
    }
