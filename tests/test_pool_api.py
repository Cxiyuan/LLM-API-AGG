from __future__ import annotations

from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import load_settings
from catalogwatch.util import utcnow_iso


def _settings(tmp_path):
    return load_settings(db_path=tmp_path / "api.db")


class _FakeResponse:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
                "credit": 0,
            },
        }


class _FakeStreamResponse:
    status_code = 200

    async def aiter_bytes(self):
        yield (
            b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"credit":0}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        return None


class _FakeBridge:
    def __init__(self):
        self.last_payload = None
        self.last_headers = None

    async def post_chat_with_headers(self, payload, headers):
        self.last_payload = payload
        self.last_headers = headers
        return _FakeResponse()

    async def open_chat_stream(self, payload, headers):
        self.last_payload = payload
        self.last_headers = headers
        return _FakeStreamResponse()


def _seed(service):
    service.store.upsert_account(
        {
            "id": 1,
            "provider": "workbuddy",
            "name": "main",
            "status": "active",
            "weight": 1,
            "priority": 0,
        }
    )
    service.store.record_credit_snapshot(
        1, {"ok": True, "available_total": 100, "total_dosage": 100}
    )
    service.store.upsert_models(
        "workbuddy",
        [{"id": "free-model", "credits": 0, "contextWindow": 100000}],
    )
    service.gateway.bridge = _FakeBridge()
    return service.gateway.bridge


def test_pool_key_and_chat_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/keys",
            headers={"Authorization": "Bearer test-admin-token"},
            json={"name": "smoke", "kind": "shared", "daily_request_limit": 0},
        )
        assert created.status_code == 200
        pool_key = created.json()["key"]
        key_id = created.json()["id"]
        auth = {"Authorization": f"Bearer {pool_key}"}

        models = client.get("/v1/models", headers=auth)
        assert models.status_code == 200
        assert any(item["id"] == "auto" for item in models.json()["data"])

        # No account yet: the pool must fail clearly, not crash.
        no_account = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert no_account.status_code == 503
        assert no_account.json()["error"]["code"] == "no_accounts"

        service = app.state.workbuddy
        fake = _seed(service)

        response = client.post(
            "/v1/chat/completions",
            headers={**auth, "X-Session-ID": "session-1"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello"
        assert response.headers["X-Pool-Model"] == "free-model"
        assert response.headers["X-Pool-Account"] == "1"
        assert fake.last_payload["model"] == "workbuddy/free-model"
        assert fake.last_headers["X-WorkBuddy-Account"] == "1"
        assert fake.last_headers["X-WorkBuddy-Account-Mode"] == "strict"
        binding = service.store.get_session("session-1")
        assert binding is not None
        assert binding["model_id"] == "free-model"
        assert binding["account_id"] == 1

        requests = service.store.list_requests()
        assert requests and requests[0]["model_routed"] == "free-model"
        assert requests[0]["status_code"] == 200
        assert requests[0]["total_tokens"] == 3

        usage = service.store.key_usage_today(key_id, utcnow_iso()[:10])
        # A request rejected before dispatch (no account/model) does not reserve
        # the key quota; only the dispatched request counts.
        assert usage["requests"] == 1
        assert usage["tokens"] == 3

        streamed = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert streamed.status_code == 200
        assert "data:" in streamed.text
        assert streamed.headers["X-Pool-Model"] == "free-model"

        # Anthropic Messages compatibility (non-stream + stream).
        anthropic = client.post(
            "/v1/messages",
            headers={**auth, "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert anthropic.status_code == 200
        assert anthropic.json()["type"] == "message"
        assert anthropic.json()["content"][0]["text"] == "hello"

        anthropic_stream = client.post(
            "/v1/messages",
            headers={**auth, "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert anthropic_stream.status_code == 200
        assert "event: message_start" in anthropic_stream.text
        assert "text_delta" in anthropic_stream.text
        assert "event: message_stop" in anthropic_stream.text


def test_pool_key_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/keys",
            headers={"Authorization": "Bearer test-admin-token"},
            json={"name": "limited", "kind": "agent", "daily_request_limit": 1},
        )
        assert created.status_code == 200
        auth = {"Authorization": f"Bearer {created.json()['key']}"}
        _seed(app.state.workbuddy)

        first = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": "auto", "messages": [{"role": "user", "content": "a"}]},
        )
        assert first.status_code == 200
        second = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": "auto", "messages": [{"role": "user", "content": "b"}]},
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "key_rate_limited"


def test_pool_rejects_direct_provider_models(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/keys",
            headers={"Authorization": "Bearer test-admin-token"},
            json={"name": "smoke", "kind": "shared", "daily_request_limit": 0},
        )
        pool_key = created.json()["key"]
        auth = {"Authorization": f"Bearer {pool_key}"}
        _seed(app.state.workbuddy)

        models = client.get("/v1/models", headers=auth).json()["data"]
        ids = {item["id"] for item in models}
        assert "auto" in ids
        assert all(not item.startswith("nvidia/") for item in ids)

        response = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "nvidia/stepfun-ai/step-5-preview",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_unavailable"


def test_auto_retries_next_model_on_context_400(tmp_path, monkeypatch):
    from catalogwatch.workbuddy.bridge import BridgeError

    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/keys",
            headers={"Authorization": "Bearer test-admin-token"},
            json={"name": "smoke", "kind": "shared", "daily_request_limit": 0},
        )
        auth = {"Authorization": f"Bearer {created.json()['key']}"}
        service = app.state.workbuddy
        for account_id in (1, 2):
            service.store.upsert_account(
                {
                    "id": account_id,
                    "provider": "workbuddy",
                    "name": f"acct-{account_id}",
                    "status": "active",
                }
            )
            service.store.record_credit_snapshot(
                account_id, {"ok": True, "available_total": 100, "total_dosage": 100}
            )
        service.store.upsert_models(
            "workbuddy",
            [
                {"id": "free-model", "credits": 0, "contextWindow": 100000},
                {"id": "paid-model", "credits": 0.2, "contextWindow": 100000},
            ],
        )

        service.store.replace_promotions(
            "workbuddy",
            {
                "paid-model": [
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

        class _Bridge:
            def __init__(self):
                self.models = []

            async def post_chat_with_headers(self, payload, headers):
                self.models.append(payload["model"])
                if payload["model"] == "workbuddy/free-model":
                    raise BridgeError(
                        400,
                        "workbuddy 上下文超过可用预算。没有更早的历史可以裁。请新开一段对话。",
                    )
                return _FakeResponse()

        bridge = _Bridge()
        service.gateway.bridge = bridge
        response = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200, response.text
        assert "workbuddy/paid-model" in bridge.models


class _EmptyStream:
    status_code = 200

    async def aiter_bytes(self):
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        return None


class _QuotaStream:
    status_code = 200

    async def aiter_bytes(self):
        yield (
            b'data: {"error":{"message":"daily quota","type":"upstream_error",'
            b'"code":"429"}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        return None


class _ContentStream:
    status_code = 200

    async def aiter_bytes(self):
        yield (
            b'data: {"choices":[{"index":0,"delta":{"content":"ok"},'
            b'"finish_reason":null}]}\n\n'
        )
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        return None


def _stream_service(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    client.__enter__()
    created = client.post(
        "/api/keys",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"name": "smoke", "kind": "shared", "daily_request_limit": 0},
    )
    auth = {"Authorization": f"Bearer {created.json()['key']}"}
    service = app.state.workbuddy
    service.store.upsert_account(
        {"id": 1, "provider": "workbuddy", "name": "acct", "status": "active"}
    )
    service.store.record_credit_snapshot(
        1, {"ok": True, "available_total": 100, "total_dosage": 100}
    )
    service.store.upsert_models(
        "workbuddy",
        [
            {"id": "free-model", "credits": 0, "contextWindow": 100000},
            {"id": "paid-model", "credits": 0.2, "contextWindow": 100000},
        ],
    )
    service.store.replace_promotions(
        "workbuddy",
        {
        "paid-model": [
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
    return client, auth, service


def test_empty_stream_is_a_failure_and_cools_the_model(tmp_path, monkeypatch):
    client, auth, service = _stream_service(tmp_path, monkeypatch)
    service.store.set_setting("policy", {"allow_night": False})

    class _Bridge:
        def __init__(self):
            self.models = []

        async def open_chat_stream(self, payload, headers):
            self.models.append(payload["model"])
            return (
                _EmptyStream()
                if payload["model"] == "workbuddy/free-model"
                else _ContentStream()
            )

    bridge = _Bridge()
    service.gateway.bridge = bridge
    try:
        first = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert first.status_code == 200
        assert bridge.models == ["workbuddy/free-model"]
        request = service.store.list_requests()[0]
        assert request["status_code"] == 502
        assert request["error_code"] == "empty_response"

        # The empty free model is now cooled down, so the next auto request
        # must fall through to the paid model instead of repeating it.
        second = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert second.status_code == 200
        assert bridge.models[-1] == "workbuddy/paid-model"
        assert second.headers["X-Pool-Model"] == "paid-model"
        assert "ok" in second.text
    finally:
        client.__exit__(None, None, None)


def test_in_stream_quota_error_keeps_its_real_status(tmp_path, monkeypatch):
    client, auth, service = _stream_service(tmp_path, monkeypatch)
    service.store.set_setting("policy", {"allow_night": False})

    class _Bridge:
        async def open_chat_stream(self, payload, headers):
            return _QuotaStream()

    service.gateway.bridge = _Bridge()
    try:
        response = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        request = service.store.list_requests()[0]
        assert request["status_code"] == 429
        assert request["error_code"] == "rate_limit"
        # A model-scoped quota 429 must not take the whole account out of
        # rotation: another model on the same account is still usable.
        state = service.store.get_state(1)
        assert not state.get("cooldown_until")
        assert service.gateway.router.load.cooling("workbuddy", "free-model")
    finally:
        client.__exit__(None, None, None)

