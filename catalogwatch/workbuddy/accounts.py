"""WorkBuddy account lifecycle against the local bridge admin API."""

from __future__ import annotations

import asyncio
from typing import Any

from .bridge import BridgeError, BuddyBridge
from .store import WorkBuddyStore


class WorkBuddyAccounts:
    def __init__(self, store: WorkBuddyStore, bridge: BuddyBridge):
        self.store = store
        self.bridge = bridge

    async def sync(self) -> list[dict]:
        rows = await self.bridge.list_accounts()
        present_ids: list[int] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            account_id = self.store.upsert_account(row)
            present_ids.append(account_id)
        self.store.mark_missing_accounts(present_ids)
        return self.store.list_accounts(present=True)

    async def refresh_credits(self, *, force: bool = True) -> dict:
        summary = await self.bridge.credit_summary(force=force)
        resources = summary.get("accounts") if isinstance(summary, dict) else None
        if not isinstance(resources, list):
            resources = []
        seen: set[int] = set()
        for item in resources:
            if not isinstance(item, dict):
                continue
            account_id = item.get("account_id")
            if account_id is None:
                continue
            try:
                account_id = int(account_id)
            except (TypeError, ValueError):
                continue
            seen.add(account_id)
            self.store.record_credit_snapshot(account_id, item)

        # If the summary did not include a resource row for an account (for
        # example a stale/failed channel), try the per-account endpoint.
        missing = [
            row["account_id"]
            for row in self.store.list_accounts(present=True)
            if int(row["account_id"]) not in seen
        ]
        if missing:
            semaphore = asyncio.Semaphore(2)

            async def one(account_id: int) -> None:
                async with semaphore:
                    try:
                        payload = await self.bridge.account_resources(
                            account_id, force=force
                        )
                    except BridgeError as exc:
                        payload = {
                            "ok": False,
                            "account_id": account_id,
                            "message": f"HTTP {exc.status_code}: {exc}"[:500],
                        }
                    self.store.record_credit_snapshot(account_id, payload)

            await asyncio.gather(*(one(account_id) for account_id in missing))

        return {
            "ok": bool(summary.get("ok", True)) if isinstance(summary, dict) else False,
            "accounts": len(resources),
            "summary": summary,
        }

    async def refresh_account_credits(self, account_id: int, *, force: bool = True) -> dict:
        payload = await self.bridge.account_resources(account_id, force=force)
        self.store.record_credit_snapshot(account_id, payload)
        return payload

    async def login_start(self, realm: str = "cn") -> dict:
        realm = "global" if str(realm).lower() == "global" else "cn"
        return await self.bridge.login_start(realm)

    async def login_poll(self, state: str) -> dict:
        result = await self.bridge.login_poll(state)
        if isinstance(result, dict) and result.get("done"):
            await self.sync()
        return result

    async def refresh_account(self, account_id: int) -> dict:
        result = await self.bridge.refresh_account(account_id)
        await self.sync()
        return result

    async def test_account(self, account_id: int, payload: dict | None = None) -> dict:
        return await self.bridge.test_account(account_id, payload or {})

    async def update_account(self, account_id: int, payload: dict) -> dict:
        result = await self.bridge.update_account(account_id, payload)
        await self.sync()
        return result

    async def delete_account(self, account_id: int) -> None:
        await self.bridge.delete_account(account_id)
        self.store.mark_account_missing(account_id)

    async def discover(self, params: dict | None = None) -> dict:
        return await self.bridge.discover_accounts(params or {})

    async def import_accounts(self, payload: dict) -> dict:
        result = await self.bridge.import_accounts(payload)
        await self.sync()
        return result

    async def checkin_status(self, *, force: bool = False) -> dict:
        return await self.bridge.checkin_status_all(force=force)

    async def checkin_one(self, account_id: int) -> dict:
        return await self.bridge.checkin_one(account_id)

    async def checkin_all(self) -> dict:
        return await self.bridge.checkin_all()

    async def keepalive_all(self) -> dict:
        return await self.bridge.keepalive_all()

    async def settings(self) -> dict:
        return await self.bridge.get_settings()

    async def update_settings(self, payload: dict) -> dict:
        return await self.bridge.update_settings(payload)

    def summary(self) -> list[dict]:
        accounts = self.store.list_accounts(present=True)
        for account in accounts:
            account["latest_credit"] = self.store.latest_credit(
                int(account["account_id"])
            )
            account["state"] = self.store.get_state(int(account["account_id"]))
        return accounts
