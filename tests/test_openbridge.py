from __future__ import annotations

import pytest

from catalogwatch.db import Database
from catalogwatch.workbuddy.catalog import WorkBuddyCatalog
from catalogwatch.workbuddy.openbridge import OpenBridgeCatalog
from catalogwatch.workbuddy.router import PoolIntent, PoolRouter
from catalogwatch.workbuddy.store import WorkBuddyStore


class _FakeOpenBridge:
    configured = True

    async def models(self) -> list[dict]:
        return [
            {"id": "chat-qwen-ai/qwen3-max", "object": "model", "owned_by": "qwen"},
            {"id": "chat-deepseek/deepseek-chat", "object": "model", "owned_by": "deepseek"},
        ]


@pytest.mark.asyncio
async def test_openbridge_models_are_quota_covered(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    catalog = OpenBridgeCatalog(store, _FakeOpenBridge())
    result = await catalog.refresh()

    assert result["models"] == 2
    stored = store.list_models("openbridge")
    assert [item["model_id"] for item in stored] == [
        "chat-deepseek/deepseek-chat",
        "chat-qwen-ai/qwen3-max",
    ]
    assert all(item["base_credits"] == 0.0 for item in stored)
    db.close()


def test_router_plans_accountless_openbridge_attempt(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    store.upsert_models(
        "openbridge",
        [{"id": "chat-qwen-ai/qwen3-max", "credits": 0.0, "credits_raw": "quota"}],
        source_identity="openbridge",
    )
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    intent = PoolIntent(
        model="auto",
        stream=True,
        tools_required=True,
        vision_required=False,
        reasoning_required=False,
        estimated_prompt_tokens=100,
        requested_max_output=100,
        context_required=1000,
    )
    attempts = router.plan(intent, {})

    assert attempts
    assert attempts[0].provider == "openbridge"
    assert attempts[0].account is None
    assert attempts[0].free is True
    assert attempts[0].wire_id == "chat-qwen-ai/qwen3-max"
    db.close()


class _FakeOBResponse:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"content": "openbridge-ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }


class _FakeOpenBridgeClient:
    configured = True

    def __init__(self):
        self.calls = []

    async def post_chat_with_headers(self, payload, headers):
        self.calls.append((payload, headers))
        return _FakeOBResponse()


class _FakeBuddy:
    async def post_chat_with_headers(self, payload, headers):
        raise AssertionError("Buddy2api must not be used for an openbridge-only plan")


@pytest.mark.asyncio
async def test_gateway_dispatches_to_openbridge(tmp_path):
    from catalogwatch.workbuddy.gateway import PoolGateway

    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    store.upsert_models(
        "openbridge",
        [{"id": "chat-qwen-ai/qwen3-max", "credits": 0.0, "credits_raw": "quota"}],
        source_identity="openbridge",
    )
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    ob = _FakeOpenBridgeClient()
    gateway = PoolGateway(store, router, _FakeBuddy(), openbridge=ob)

    status, body, _headers = await gateway.chat(
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, {}
    )

    assert status == 200
    assert body["choices"][0]["message"]["content"] == "openbridge-ok"
    assert ob.calls and ob.calls[0][0]["model"] == "chat-qwen-ai/qwen3-max"
    db.close()


@pytest.mark.asyncio
async def test_configure_openbridge_hot_reloads_client(tmp_path):
    from catalogwatch.workbuddy.service import WorkBuddyService

    db = Database(tmp_path / "catalog.db")
    service = WorkBuddyService(db)
    try:
        result = await service.configure_openbridge(url="http://127.0.0.1:4319/")
        assert result["configured"] is True
        assert result["url"] == "http://127.0.0.1:4319"
        assert service.openbridge.base_url == "http://127.0.0.1:4319"
        assert service.openbridge_catalog.client is service.openbridge
        assert service.gateway.openbridge is service.openbridge
        with pytest.raises(ValueError):
            await service.configure_openbridge(url="ftp://bad")
        disabled = await service.configure_openbridge(url="")
        assert disabled["configured"] is False
    finally:
        await service.aclose()
        db.close()
