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


def _router_with_models(tmp_path, models):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    _account(store, 1, 100)
    store.upsert_models("workbuddy", models)
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    key = {"id": 1, "allowed_models": None, "max_attempts": 3, "daily_request_limit": 0}
    return db, router, key


def test_router_rejects_non_workbuddy_provider_ids(tmp_path):
    db, router, key = _router_with_models(
        tmp_path, [{"id": "free-model", "credits": 0, "contextWindow": 100000}]
    )
    try:
        for model in ("nvidia/stepfun-ai/step-5-preview", "groq/llama-3.3-70b"):
            intent = extract_intent(
                {"model": model, "messages": [{"role": "user", "content": "hi"}]}
            )
            with pytest.raises(PoolError) as exc:
                router.plan(intent, key)
            assert exc.value.code == "model_unavailable"
            assert exc.value.status_code == 404
    finally:
        db.close()


def test_router_accepts_explicit_workbuddy_wire_id(tmp_path):
    db, router, key = _router_with_models(
        tmp_path, [{"id": "free-model", "credits": 0, "contextWindow": 100000}]
    )
    try:
        intent = extract_intent(
            {"model": "workbuddy/free-model", "messages": [{"role": "user", "content": "hi"}]}
        )
        attempts = router.plan(intent, key)
        assert attempts[0].model_id == "free-model"
        assert attempts[0].wire_id == "workbuddy/free-model"
        assert attempts[0].account["account_id"] == 1
    finally:
        db.close()


def test_router_rejects_non_text_explicit_model(tmp_path):
    db, router, key = _router_with_models(
        tmp_path, [{"id": "free-model", "credits": 0, "contextWindow": 100000}]
    )
    try:
        intent = extract_intent(
            {"model": "workbuddy/kling-video", "messages": [{"role": "user", "content": "hi"}]}
        )
        with pytest.raises(PoolError) as exc:
            router.plan(intent, key)
        assert exc.value.code == "model_not_text"
        assert exc.value.status_code == 400
    finally:
        db.close()


def test_router_keeps_diffusion_models_out_of_tool_requests(tmp_path):
    db, router, key = _router_with_models(
        tmp_path,
        [
            {"id": "diffusiongemma-26b", "credits": 0, "contextWindow": 100000},
            {"id": "tool-model", "credits": 0.5, "contextWindow": 100000},
        ],
    )
    try:
        intent = extract_intent(
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "ping", "parameters": {}}}],
            }
        )
        attempts = router.plan(intent, key)
        assert [item.model_id for item in attempts] == ["tool-model"]
    finally:
        db.close()


def test_cost_mode_orders_paid_models_by_current_multiplier(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    _account(store, 1, 100)
    store.upsert_models(
        "workbuddy",
        [
            {"id": "expensive", "credits": 0.8, "contextWindow": 100000},
            {"id": "cheaper", "credits": 0.1, "contextWindow": 100000},
        ],
    )
    store.set_setting("policy", {"routing_mode": "cost"})
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    key = {"id": 1, "allowed_models": None, "max_attempts": 3, "daily_request_limit": 0}
    intent = extract_intent({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    attempts = router.plan(intent, key)
    assert attempts[0].model_id == "cheaper"
    db.close()


def test_auto_prefers_free_then_current_discount(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    _account(store, 1, 100)
    _account(store, 2, 100)
    _account(store, 3, 100)
    store.upsert_models(
        "workbuddy",
        [
            {"id": "free-model", "credits": 0.0, "contextWindow": 100000},
            {"id": "discounted", "credits": 0.2, "contextWindow": 100000},
            {"id": "plain", "credits": 0.15, "contextWindow": 100000},
        ],
    )
    store.replace_promotions(
        "workbuddy",
        {
            "discounted": [
                {
                    "label": "限时折扣",
                    "color": None,
                    "factor": 0.5,
                    "priority": 0,
                    "active_only": False,
                    "schedule": None,
                    "text_zh": "",
                    "text_en": "",
                }
            ]
        },
    )
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    key = {"id": 1, "allowed_models": None, "max_attempts": 3, "daily_request_limit": 0}
    intent = extract_intent({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    ranked = router._rank_models(intent, key, router.policy())
    # free (0) -> discounted (0.2 * 0.5 = 0.1) -> plain (0.15)
    assert [item["model_id"] for item in ranked[:3]] == ["free-model", "discounted", "plain"]
    db.close()


def test_accounts_are_balanced_across_idle_pool_members(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    _account(store, 1, 100)
    _account(store, 2, 100)
    store.upsert_models(
        "workbuddy",
        [{"id": "free-model", "credits": 0.0, "contextWindow": 100000}],
    )
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    key = {"id": 1, "allowed_models": None, "max_attempts": 3, "daily_request_limit": 0}
    intent = extract_intent({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})

    first = router.plan(intent, key, session_id="session-a")
    first_account = int(first[0].account["account_id"])
    # Mark the first account busy; a different session must go to the idle one.
    router.load.enter("workbuddy", "free-model", first_account)
    second = router.plan(intent, key, session_id="session-b")
    second_account = int(second[0].account["account_id"])
    assert second_account != first_account
    db.close()
