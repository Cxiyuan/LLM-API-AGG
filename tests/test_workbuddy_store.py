from __future__ import annotations

from catalogwatch.db import Database
from catalogwatch.workbuddy.store import WorkBuddyStore


def test_account_model_credit_and_key_storage(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)

    account_id = store.upsert_account(
        {
            "id": 7,
            "provider": "workbuddy",
            "name": "Main",
            "nickname": "main",
            "status": "active",
            "weight": 1,
            "priority": 0,
            "credit_limit": 0,
            "credit_baseline": 0,
            "total_credits": 0,
        }
    )
    assert account_id == 7
    accounts = store.list_accounts()
    assert accounts[0]["account_id"] == 7
    assert store.get_state(7)["health"] == "unknown"

    store.record_credit_snapshot(
        7,
        {
            "ok": True,
            "available_total": 120.5,
            "total_dosage": 130,
            "expiring_7d_total": 10,
            "expiring_30d_total": 20,
            "package_count": 2,
            "packages": [{"name": "free"}],
        },
    )
    account = store.get_account(7)
    assert account["available_credits"] == 120.5
    latest = store.latest_credit(7)
    assert latest["available_total"] == 120.5

    store.upsert_models(
        "workbuddy",
        [
            {
                "id": "glm-5.2",
                "name": "GLM-5.2",
                "credits": "x0.79",
                "contextWindow": 300000,
                "supportsToolCall": True,
            }
        ],
        source_identity="current",
    )
    model = store.get_model("workbuddy", "glm-5.2")
    assert model["base_credits"] == 0.79
    assert model["capabilities"]["supportsToolCall"] is True

    store.replace_promotions(
        "workbuddy",
        {
            "glm-5.2": [
                {
                    "label": "夜间折扣",
                    "color": None,
                    "factor": 0.5,
                    "priority": 0,
                    "active_only": False,
                    "schedule": {"daily": [{"start": "23:00", "end": "08:00"}], "timezone": "Asia/Shanghai"},
                    "text_zh": "",
                    "text_en": "",
                }
            ]
        },
        source_identity="current",
    )
    rules = store.get_promotions("workbuddy", "glm-5.2")
    assert len(rules) == 1 and rules[0]["factor"] == 0.5

    store.upsert_manual_cost(
        "workbuddy",
        "glm-5.2",
        base_credits=1.0,
        factor=0,
        daily_windows=[{"start": "00:00", "end": "23:59"}],
        timezone_name="Asia/Shanghai",
        valid_from=None,
        valid_until=None,
    )
    manual = store.get_manual_cost("workbuddy", "glm-5.2")
    assert manual["base_credits"] == 1.0 and manual["factor"] == 0

    key_id = store.create_pool_key(
        name="agent",
        key_hash="hash",
        key_prefix="sk-cw-test",
        kind="agent",
        allowed_models=None,
        daily_request_limit=1,
        daily_credit_limit=0,
        max_attempts=3,
    )
    assert store.reserve_key_request(key_id, "2026-10-30", 1) is True
    assert store.reserve_key_request(key_id, "2026-10-30", 1) is False
    assert store.get_pool_key(key_id)["name"] == "agent"

    store.start_request(
        request_id="req-1",
        pool_key_id=key_id,
        endpoint="chat",
        model_requested="auto",
        stream=False,
        intent={"prompt_tokens": 10},
        routing=[{"account_id": 7}],
    )
    store.finish_request(
        "req-1",
        model_routed="glm-5.2",
        bridge_account_id=7,
        total_tokens=10,
        credits=0,
        status_code=200,
        attempts=1,
    )
    assert store.get_request("req-1")["model_routed"] == "glm-5.2"

    db.close()


def test_reopening_database_keeps_pool_tables(tmp_path):
    path = tmp_path / "catalog.db"
    Database(path).close()
    db = Database(path)
    store = WorkBuddyStore(db)
    assert store.list_accounts() == []
    db.close()
