"""WorkBuddy pool service: bridge client + periodic catalog/credit loops."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from ..config import Settings
from ..db import Database
from ..util import utcnow_iso
from .accounts import WorkBuddyAccounts
from .bridge import BuddyBridge
from .catalog import WorkBuddyCatalog, cost_summary
from .gateway import PoolGateway
from .reconcile import Reconciler
from .router import PoolRouter
from .store import WorkBuddyStore

log = logging.getLogger(__name__)


def _seconds(store: WorkBuddyStore, key: str, default: int, minimum: int, maximum: int) -> int:
    value = store.get_setting(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class WorkBuddyService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.store = WorkBuddyStore(db)
        self.bridge = BuddyBridge(
            base_url=os.environ.get("BUDDY2API_URL", "http://127.0.0.1:8787"),
            admin_token=os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
            gateway_key=os.environ.get("WORKBUDDY_GATEWAY_KEY", ""),
        )
        self.catalog = WorkBuddyCatalog(self.store, self.bridge)
        self.accounts = WorkBuddyAccounts(self.store, self.bridge)
        self.router = PoolRouter(self.store, self.catalog)
        self.gateway = PoolGateway(self.store, self.router, self.bridge)
        self.reconciler = Reconciler(self.store, self.bridge)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def aclose(self) -> None:
        await self.bridge.aclose()

    async def refresh_catalog(self, *, force: bool = False) -> list[dict]:
        async with self._lock("catalog"):
            return await self.catalog.refresh(force=force)

    async def refresh_accounts(self) -> list[dict]:
        async with self._lock("accounts"):
            return await self.accounts.sync()

    async def refresh_credits(self, *, force: bool = True) -> dict:
        async with self._lock("credits"):
            return await self.accounts.refresh_credits(force=force)

    async def refresh_all(self, *, force: bool = True) -> dict:
        accounts = await self.refresh_accounts()
        try:
            credits = await self.refresh_credits(force=force)
        except Exception as exc:  # noqa: BLE001 - keep the other jobs alive
            log.warning("workbuddy credit refresh failed: %s", exc)
            credits = {"ok": False, "error": str(exc)}
        try:
            catalog = await self.refresh_catalog(force=force)
        except Exception as exc:  # noqa: BLE001
            log.warning("workbuddy catalog refresh failed: %s", exc)
            catalog = [{"ok": False, "warning": str(exc)}]
        return {"accounts": len(accounts), "credits": credits, "catalog": catalog}

    async def due_work(self) -> None:
        store = self.store
        now = time.monotonic()
        account_interval = _seconds(store, "account_sync_seconds", 60, 15, 900)
        credit_interval = _seconds(store, "credit_refresh_seconds", 300, 60, 3600)
        catalog_interval = _seconds(store, "catalog_refresh_seconds", 60, 30, 600)
        last = getattr(self, "_last", None)
        if last is None:
            last = {
                "accounts": 0.0,
                "credits": 0.0,
                "catalog": 0.0,
                "reconcile": 0.0,
                "sessions": 0.0,
            }
            self._last = last

        try:
            if now - last["accounts"] >= account_interval:
                await self.refresh_accounts()
                last["accounts"] = now
        except Exception:  # noqa: BLE001
            log.exception("workbuddy account sync failed")
        try:
            has_accounts = bool(self.store.list_accounts(present=True))
        except Exception:  # noqa: BLE001
            has_accounts = False
        try:
            if has_accounts and now - last["credits"] >= credit_interval:
                await self.refresh_credits(force=True)
                last["credits"] = now
        except Exception:  # noqa: BLE001
            log.exception("workbuddy credit refresh failed")
        try:
            if has_accounts and now - last["catalog"] >= catalog_interval:
                await self.refresh_catalog(force=True)
                last["catalog"] = now
        except Exception:  # noqa: BLE001
            log.exception("workbuddy catalog refresh failed")
        try:
            if now - last["reconcile"] >= 30:
                async with self._lock("reconcile"):
                    await self.reconciler.reconcile()
                last["reconcile"] = now
        except Exception:  # noqa: BLE001
            log.exception("workbuddy ledger reconciliation failed")
        try:
            if now - last["sessions"] >= 3600:
                store.cleanup_sessions(utcnow_iso())
                last["sessions"] = now
        except Exception:  # noqa: BLE001
            log.exception("workbuddy session cleanup failed")

    async def run_forever(self, stop: asyncio.Event, interval_seconds: int = 10) -> None:
        log.info("workbuddy pool scheduler started (tick=%ss)", interval_seconds)
        while not stop.is_set():
            try:
                await self.due_work()
            except Exception:  # noqa: BLE001 - keep the pool scheduler alive
                log.exception("workbuddy pool cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
        log.info("workbuddy pool scheduler stopped")

    # ------------------------------------------------------------------- summaries

    def overview(self) -> dict:
        accounts = self.store.list_accounts(present=True)
        states = {int(a["account_id"]): self.store.get_state(int(a["account_id"])) for a in accounts}
        total_available = sum(
            float(a.get("available_credits") or 0) for a in accounts
        )
        free_models = [item for item in self.catalog.list_costs() if item.get("free")]
        cooldowns = [
            {
                "account_id": account_id,
                "until": state.get("cooldown_until") or state.get("free_exhausted_until"),
                "last_error": state.get("last_error"),
            }
            for account_id, state in states.items()
            if state.get("cooldown_until") or state.get("free_exhausted_until")
        ]
        return {
            "accounts": len(accounts),
            "available_credits": round(total_available, 4),
            "free_models": len(free_models),
            "models": len(self.catalog.list_costs()),
            "cooldowns": cooldowns,
            "cost_summary": cost_summary(free_models),
            "updated_at": utcnow_iso(),
        }
