from __future__ import annotations

import pytest

from catalogwatch.db import Database
from catalogwatch.workbuddy.catalog import WorkBuddyCatalog
from catalogwatch.workbuddy.router import PoolError, PoolRouter, extract_intent
from catalogwatch.workbuddy.store import WorkBuddyStore


def _account(store: WorkBuddyStore, account_id: int, credits: float):
    store.upsert_account(
        {
            "id": account_id,
            "provider": "workbuddy",
            "name": f"acct-{account_id}",
            "status": "active",
            "weight": 1,
            "priority": 0,
        }
    )
    store.record_credit_snapshot(
        account_id,
        {"ok": True, "available_total": credits, "total_dosage": credits},
    )


def test_router_prefers_free_then_low_multiplier(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    _account(store, 1, 100)
    _account(store, 2, 10)
    store.upsert_models(
        "workbuddy",
        [
            {"id": "free-model", "credits": 0, "contextWindow": 100000},
            {"id": "paid-model", "credits": 0.5, "contextWindow": 100000},
        ],
    )
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    key = {"id": 1, "allowed_models": None, "max_attempts": 3, "daily_request_limit": 0}

    intent = extract_intent({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    attempts = router.plan(intent, key)
    assert attempts[0].model_id == "free-model"
    assert attempts[0].free is True
    assert attempts[0].account["account_id"] == 1

    explicit = extract_intent(
        {"model": "paid-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    paid = router.plan(explicit, key)
    assert paid[0].model_id == "paid-model"
    assert paid[0].account["account_id"] == 1  # richer account first
    db.close()


def test_router_rejects_when_no_accounts(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    intent = extract_intent({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    with pytest.raises(PoolError) as exc:
        router.plan(intent, {"id": 1, "allowed_models": None, "max_attempts": 3})
    assert exc.value.code == "no_accounts"
    db.close()
