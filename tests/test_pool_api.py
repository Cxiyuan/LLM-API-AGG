from __future__ import annotations

from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import load_settings
from catalogwatch.util import utcnow_iso


def _settings(tmp_path):
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  - id: demo
    name: Demo
    adapter: openai_models
    base_url: https://example.com/v1
    key_required: false
    free_kind: free_tier
""",
        encoding="utf-8",
    )
    return load_settings(config, db_path=tmp_path / "api.db")


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
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
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
            headers=auth,
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello"
        assert response.headers["X-Pool-Model"] == "free-model"
        assert response.headers["X-Pool-Account"] == "1"
        assert fake.last_payload["model"] == "workbuddy/free-model"
        assert fake.last_headers["X-WorkBuddy-Account"] == "1"
        assert fake.last_headers["X-WorkBuddy-Account-Mode"] == "strict"

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


def test_pool_key_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
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
