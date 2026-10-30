from __future__ import annotations

from catalogwatch.db import Database
from catalogwatch.util import utcnow_iso
from catalogwatch.workbuddy.reconcile import Reconciler
from catalogwatch.workbuddy.store import WorkBuddyStore


class _FakeBridge:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def search_logs(self, **params):
        self.calls.append(params)
        return {"items": self.items}


async def test_pending_request_is_reconciled_from_bridge_log(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    store.upsert_account(
        {
            "id": 1,
            "provider": "workbuddy",
            "name": "main",
            "status": "active",
        }
    )
    key_id = store.create_pool_key(
        name="agent",
        key_hash="hash",
        key_prefix="sk-cw-test",
        kind="agent",
        allowed_models=None,
        daily_request_limit=0,
        daily_credit_limit=0,
        max_attempts=3,
    )
    day = utcnow_iso()[:10]
    assert store.reserve_key_request(key_id, day, 0)
    store.start_request(
        request_id="req-1",
        pool_key_id=key_id,
        endpoint="chat",
        model_requested="auto",
        stream=False,
        intent={},
        routing=[],
    )
    store.record_attempt(
        request_id="req-1",
        attempt_no=1,
        account_id=1,
        model_id="free-model",
        provider="workbuddy",
        status_code=200,
        error_code=None,
        started_at=utcnow_iso(),
        duration_ms=10,
        credits=None,
    )
    store.finish_request(
        "req-1",
        model_routed="free-model",
        provider="workbuddy",
        bridge_account_id=1,
        total_tokens=0,
        credits=None,
        cost_source="pending",
        status_code=200,
        attempts=1,
    )

    fake = _FakeBridge(
        [
            {
                "client_request_id": "req-1:1",
                "status_code": 200,
                "prompt_tokens": 4,
                "completion_tokens": 6,
                "total_tokens": 10,
                "credit": 2.5,
                "finish_reason": "stop",
            }
        ]
    )
    result = await Reconciler(store, fake).reconcile(older_than_seconds=0)
    assert result["checked"] == 1
    assert result["reconciled"] == 1
    assert fake.calls and fake.calls[0]["client_request_id"] == "req-1:1"

    request = store.get_request("req-1")
    assert request["cost_source"] == "reconciled"
    assert request["credits"] == 2.5
    assert request["total_tokens"] == 10

    usage = store.key_usage_today(key_id, day)
    assert usage["tokens"] == 10
    assert usage["credits"] == 2.5

    state = store.get_state(1)
    assert state["daily_tokens"] == 10
    assert state["daily_credits"] == 2.5

    attempts = store.list_attempts("req-1")
    assert attempts[0]["credits"] == 2.5
    db.close()


async def test_missing_log_marks_unknown(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    store.start_request(
        request_id="req-2",
        pool_key_id=None,
        endpoint="chat",
        model_requested="auto",
        stream=False,
        intent={},
        routing=[],
    )
    store.finish_request("req-2", cost_source="pending", attempts=1)
    result = await Reconciler(store, _FakeBridge([])).reconcile(older_than_seconds=0)
    assert result["unknown"] == 1
    assert store.get_request("req-2")["cost_source"] == "unknown"
    db.close()
