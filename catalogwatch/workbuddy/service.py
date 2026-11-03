"""Unified gateway service: bridge/channel clients + periodic catalog/credit loops."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..db import Database
from ..util import utcnow_iso
from .accounts import WorkBuddyAccounts
from .bridge import BuddyBridge
from .catalog import WorkBuddyCatalog, cost_summary
from .providers import BUDDY_PROVIDER_ORDER, OPENBRIDGE_PROVIDER
from .gateway import PoolGateway
from .openbridge import OpenBridgeCatalog, OpenBridgeClient
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


def _configured_providers() -> tuple[str, ...]:
    """Channels the pool should scan; unknown/disabled names are ignored.

    Buddy2api still decides whether a channel is loaded.  Keeping this list
    explicit means a bridge upgrade cannot silently make the pool route a
    channel whose billing/account shape we have not reviewed.
    """
    raw = os.environ.get("WORKBUDDY_PROVIDERS", "").strip()
    if not raw:
        return BUDDY_PROVIDER_ORDER
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    return tuple(provider for provider in BUDDY_PROVIDER_ORDER if provider in wanted)


class WorkBuddyService:
    def __init__(self, db: Database):
        self.store = WorkBuddyStore(db)
        try:
            bridge_timeout = float(
                os.environ.get("WORKBUDDY_BRIDGE_TIMEOUT_SECONDS", "300") or "300"
            )
        except ValueError:
            bridge_timeout = 300.0
        self.bridge = BuddyBridge(
            base_url=os.environ.get("BUDDY2API_URL", "http://127.0.0.1:8787"),
            admin_token=os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
            gateway_key=os.environ.get("WORKBUDDY_GATEWAY_KEY", ""),
            timeout=bridge_timeout,
        )
        self.catalog = WorkBuddyCatalog(
            self.store, self.bridge, providers=_configured_providers()
        )
        self.accounts = WorkBuddyAccounts(self.store, self.bridge)
        self.openbridge = OpenBridgeClient(
            os.environ.get("OPENBRIDGE_URL", ""),
            api_key=os.environ.get("OPENBRIDGE_API_KEY", ""),
            timeout=bridge_timeout,
        )
        self.openbridge_catalog = OpenBridgeCatalog(self.store, self.openbridge)
        self.router = PoolRouter(self.store, self.catalog)
        self.gateway = PoolGateway(
            self.store, self.router, self.bridge, openbridge=self.openbridge
        )
        self.reconciler = Reconciler(self.store, self.bridge)
        self._locks: dict[str, asyncio.Lock] = {}
        self._bridge_auto_checkin_off = False
        self._bridge_failures = 0

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def aclose(self) -> None:
        await self.bridge.aclose()
        await self.openbridge.aclose()

    async def refresh_catalog(self, *, force: bool = False) -> list[dict]:
        async with self._lock("catalog"):
            results = list(await self.catalog.refresh(force=force))
            results.append(await self.openbridge_catalog.refresh())
            return results

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

    # --------------------------------------------------------------- check-in

    CHECKIN_TZ = ZoneInfo("Asia/Shanghai")

    def checkin_global(self) -> dict:
        hour = self.store.get_setting("auto_checkin_hour", 9)
        minute = self.store.get_setting("auto_checkin_minute", 0)
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            hour = 9
        try:
            minute = int(minute)
        except (TypeError, ValueError):
            minute = 0
        return {
            "enabled": bool(self.store.get_setting("auto_checkin_enabled", False)),
            "hour": hour,
            "minute": minute,
        }

    async def checkin_overview(self, *, force: bool = False) -> dict:
        today = datetime.now(self.CHECKIN_TZ).date().isoformat()
        accounts = self.store.list_accounts(present=True)
        try:
            bridge = await asyncio.wait_for(
                self.accounts.checkin_status(force=force), timeout=15.0
            )
        except asyncio.TimeoutError:
            bridge = {"error": "check-in status timeout", "results": []}
        except Exception as exc:  # noqa: BLE001 - status is best effort
            bridge = {"error": str(exc)[:300], "results": []}
        status_by_id: dict[int, dict] = {}
        if isinstance(bridge, dict):
            for row in bridge.get("results") or []:
                if isinstance(row, dict) and row.get("account_id") is not None:
                    try:
                        status_by_id[int(row["account_id"])] = row
                    except (TypeError, ValueError):
                        continue
        rules = self.store.get_checkin_rules()
        global_rule = self.checkin_global()
        items = []
        for account in accounts:
            account_id = int(account["account_id"])
            rule = rules.get(account_id)
            effective = rule or global_rule
            state = self.store.get_checkin_state(account_id) or {}
            row_status = status_by_id.get(account_id) or {}
            checked = bool(
                row_status.get("today_checked_in")
                or row_status.get("already_claimed")
                or (state.get("last_date") == today and state.get("last_status") in {"claimed", "already", "checked"})
            )
            items.append(
                {
                    "account_id": account_id,
                    "name": account.get("nickname") or account.get("name") or f"#{account_id}",
                    "provider": account.get("provider") or "workbuddy",
                    "online": bool(account.get("present", 1)),
                    "status": account.get("status") or "active",
                    "rule": {
                        "source": "account" if rule else "global",
                        "enabled": bool(effective.get("enabled")),
                        "hour": int(effective.get("hour") or 0),
                        "minute": int(effective.get("minute") or 0),
                    },
                    "checkin": {
                        **row_status,
                        "checked_today": checked,
                        "state": state,
                    },
                }
            )
        return {"global": global_rule, "accounts": items, "summary": bridge}

    async def run_checkin(self, account_id: int) -> dict:
        result = await self.accounts.checkin_one(account_id)
        today = datetime.now(self.CHECKIN_TZ).date().isoformat()
        if result.get("claimed"):
            status = "claimed"
        elif result.get("today_checked_in") or result.get("already_claimed"):
            status = "already"
        elif result.get("ok"):
            status = "checked"
        else:
            status = "failed"
        self.store.set_checkin_state(
            account_id,
            date=today,
            status=status,
            message=str(result.get("message") or "")[:300],
            attempted_at=utcnow_iso(),
            checked_at=utcnow_iso() if status in {"claimed", "already", "checked"} else None,
        )
        self.store.audit(
            "system",
            "checkin.run",
            str(account_id),
            {"status": status, "message": str(result.get("message") or "")[:200]},
        )
        return result

    async def run_all_checkins(self) -> dict:
        results = []
        for account in self.store.list_accounts(present=True):
            account_id = int(account["account_id"])
            try:
                result = await self.run_checkin(account_id)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                result = {"ok": False, "account_id": account_id, "message": str(exc)[:300]}
            results.append(result)
        return {
            "total": len(results),
            "claimed": sum(1 for row in results if row.get("claimed")),
            "results": results,
        }

    async def auto_checkin_tick(self) -> None:
        now = datetime.now(self.CHECKIN_TZ)
        today = now.date().isoformat()
        global_rule = self.checkin_global()
        rules = self.store.get_checkin_rules()
        due: list[int] = []
        for account in self.store.list_accounts(present=True):
            if str(account.get("status") or "active") != "active":
                continue
            account_id = int(account["account_id"])
            rule = rules.get(account_id)
            effective = rule or global_rule
            if not effective.get("enabled"):
                continue
            hour = int(effective.get("hour") or 0)
            minute = int(effective.get("minute") or 0)
            if (now.hour, now.minute) < (hour, minute):
                continue
            state = self.store.get_checkin_state(account_id) or {}
            if state.get("last_date") == today:
                if state.get("last_status") in {"claimed", "already", "checked"}:
                    continue
                attempted = state.get("last_attempt_at")
                if attempted:
                    try:
                        moment = datetime.fromisoformat(attempted)
                        if moment.tzinfo is None:
                            moment = moment.replace(tzinfo=timezone.utc)
                        if moment > datetime.now(timezone.utc) - timedelta(minutes=30):
                            continue
                    except ValueError:
                        pass
            due.append(account_id)
        if not due:
            return
        try:
            status = await self.accounts.checkin_status(force=False)
        except Exception:  # noqa: BLE001 - claim still works without the pre-check
            status = {}
        status_by_id: dict[int, dict] = {}
        for row in (status.get("results") if isinstance(status, dict) else None) or []:
            if isinstance(row, dict) and row.get("account_id") is not None:
                try:
                    status_by_id[int(row["account_id"])] = row
                except (TypeError, ValueError):
                    continue
        for account_id in due:
            row_status = status_by_id.get(account_id) or {}
            if row_status.get("today_checked_in") or row_status.get("already_claimed"):
                self.store.set_checkin_state(
                    account_id,
                    date=today,
                    status="already",
                    message=str(row_status.get("message") or "今日已签到")[:300],
                    attempted_at=utcnow_iso(),
                    checked_at=utcnow_iso(),
                )
                continue
            try:
                await self.run_checkin(account_id)
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                log.warning("auto check-in failed for account %s: %s", account_id, exc)
                self.store.set_checkin_state(
                    account_id,
                    date=today,
                    status="failed",
                    message=str(exc)[:300],
                    attempted_at=utcnow_iso(),
                )

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
                "checkin": 0.0,
                "bridge": 0.0,
            }
            self._last = last

        try:
            if now - last["bridge"] >= 30:
                last["bridge"] = now
                try:
                    await asyncio.wait_for(self.bridge.health(), timeout=5.0)
                    self._bridge_failures = 0
                except Exception:  # noqa: BLE001 - watchdog must never stop the scheduler
                    self._bridge_failures += 1
                    if self._bridge_failures >= 6:
                        log.error(
                            "Buddy2api health failed %s consecutive probes; resetting the HTTP client",
                            self._bridge_failures,
                        )
                        try:
                            await self.bridge.reset_client()
                        except Exception:  # noqa: BLE001
                            log.exception("could not reset the Buddy2api HTTP client")
                        self._bridge_failures = 0
        except Exception:  # noqa: BLE001
            log.exception("workbuddy bridge watchdog failed")

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
            if has_accounts and now - last["checkin"] >= 60:
                if not self._bridge_auto_checkin_off:
                    try:
                        await self.accounts.update_settings({"auto_checkin_enabled": False})
                        self._bridge_auto_checkin_off = True
                    except Exception:  # noqa: BLE001 - pool scheduler still works
                        log.warning("could not disable the bridge auto check-in")
                await self.auto_checkin_tick()
                last["checkin"] = now
        except Exception:  # noqa: BLE001
            log.exception("workbuddy auto check-in tick failed")
        try:
            if now - last["sessions"] >= 3600:
                store.cleanup_admin_sessions(utcnow_iso())
                store.cleanup_pool_sessions(utcnow_iso())
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
        # 一次取全量成本，供「模型数 / 免费数 / 成本概览」共用；概览以前只把
        # 免费子集传给 cost_summary，导致 total 显示成免费数。
        costs = self.catalog.list_costs()
        free_models = [item for item in costs if item.get("free")]
        openbridge_models = [
            item for item in costs if str(item.get("provider") or "") == OPENBRIDGE_PROVIDER
        ]
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
            "models": len(costs),
            "openbridge": {
                "configured": self.openbridge.configured,
                "models": len(openbridge_models),
            },
            "sessions": len(self.store.list_sessions(limit=1000)),
            "cooldowns": cooldowns,
            "cost_summary": cost_summary(costs),
            "updated_at": utcnow_iso(),
        }
