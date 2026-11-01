from __future__ import annotations

import pytest

from catalogwatch.db import Database
from catalogwatch.workbuddy.service import WorkBuddyService
from catalogwatch.workbuddy.store import WorkBuddyStore


def _service(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    store.upsert_account(
        {"id": 1, "provider": "workbuddy", "name": "cn", "status": "active"}
    )
    store.upsert_account(
        {"id": 2, "provider": "workbuddy-i", "name": "global", "status": "active"}
    )
    return db, store, WorkBuddyService(db)


def test_checkin_rules_override_global(tmp_path):
    db, store, service = _service(tmp_path)
    try:
        store.set_setting("auto_checkin_enabled", True)
        store.set_setting("auto_checkin_hour", 9)
        store.set_setting("auto_checkin_minute", 30)
        store.set_checkin_rule(2, enabled=True, hour=23, minute=5)
        rules = store.get_checkin_rules()
        assert rules[2]["hour"] == 23 and rules[2]["minute"] == 5
        store.delete_checkin_rule(2)
        assert store.get_checkin_rule(2) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_checkin_overview_merges_rules_and_status(tmp_path):
    db, store, service = _service(tmp_path)
    try:
        store.set_setting("auto_checkin_enabled", True)
        store.set_setting("auto_checkin_hour", 9)
        store.set_setting("auto_checkin_minute", 0)
        store.set_checkin_rule(2, enabled=True, hour=23, minute=5)

        async def status(force: bool = False) -> dict:
            return {
                "results": [
                    {"account_id": 1, "ok": True, "today_checked_in": True, "message": "今日已领取"},
                    {"account_id": 2, "ok": True, "today_checked_in": False, "message": "待领取"},
                ]
            }

        service.accounts.checkin_status = status  # type: ignore[method-assign]
        overview = await service.checkin_overview(force=True)
        by_id = {item["account_id"]: item for item in overview["accounts"]}
        assert by_id[1]["rule"]["source"] == "global"
        assert by_id[1]["rule"]["hour"] == 9
        assert by_id[1]["checkin"]["checked_today"] is True
        assert by_id[2]["rule"]["source"] == "account"
        assert by_id[2]["rule"]["hour"] == 23
        assert by_id[2]["checkin"]["checked_today"] is False
    finally:
        await service.aclose()
        db.close()


@pytest.mark.asyncio
async def test_auto_checkin_tick_claims_due_accounts(tmp_path):
    db, store, service = _service(tmp_path)
    try:
        store.set_setting("auto_checkin_enabled", True)
        store.set_setting("auto_checkin_hour", 0)
        store.set_setting("auto_checkin_minute", 0)

        async def status(force: bool = False) -> dict:
            return {"results": []}

        claimed: list[int] = []

        async def claim(account_id: int) -> dict:
            claimed.append(account_id)
            return {"ok": True, "claimed": True, "account_id": account_id, "message": "领取成功"}

        service.accounts.checkin_status = status  # type: ignore[method-assign]
        service.accounts.checkin_one = claim  # type: ignore[method-assign]
        await service.auto_checkin_tick()
        assert sorted(claimed) == [1, 2]
        state = store.get_checkin_state(1)
        assert state is not None and state["last_status"] == "claimed"
    finally:
        await service.aclose()
        db.close()
