"""WorkBuddy 只是一个来源：免费厂商直连推理、全局路由与会话亲和。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import load_settings
from catalogwatch.db import Database
from catalogwatch.workbuddy.catalog import WorkBuddyCatalog
from catalogwatch.workbuddy.direct import DirectError, DirectSource
from catalogwatch.workbuddy.router import PoolError, PoolRouter, extract_intent
from catalogwatch.workbuddy.store import WorkBuddyStore


def _settings(tmp_path):
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  - id: groq
    name: Groq
    adapter: openai_models
    base_url: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY
    free_kind: free_tier
  - id: google
    name: Google Gemini
    adapter: google_models
    base_url: https://generativelanguage.googleapis.com/v1beta
    key_env: GEMINI_API_KEY
    auth: query
    chat_base_url: https://generativelanguage.googleapis.com/v1beta/openai
    chat_auth: bearer
    free_kind: free_tier
  - id: workbuddy
    name: WorkBuddy
    adapter: workbuddy
    base_url: http://127.0.0.1:8787/v1
    key_env: WORKBUDDY_GATEWAY_KEY
    chat_enabled: false
    enabled: false
""",
        encoding="utf-8",
    )
    return load_settings(config, db_path=tmp_path / "direct.db")


def _catalog_row(
    db: Database,
    provider: str,
    model_id: str,
    *,
    name: str | None = None,
    free: bool = True,
    context: int = 32_000,
) -> None:
    now = "2026-10-30T00:00:00+00:00"
    with db.conn:
        db.conn.execute(
            "INSERT INTO models (provider_id, model_id, name, context_length, max_output,"
            " is_free, free_kind, fingerprint, first_seen, last_seen, present)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
            (
                provider,
                model_id,
                name or model_id,
                context,
                4096,
                1 if free else 0,
                "free_tier" if free else "paid",
                f"{provider}:{model_id}",
                now,
                now,
            ),
        )


def _store(tmp_path) -> WorkBuddyStore:
    return WorkBuddyStore(Database(tmp_path / "direct.db"))


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")


def test_candidates_only_free_text_models_with_keys(tmp_path):
    settings = _settings(tmp_path)
    db = Database(tmp_path / "direct.db")
    store = WorkBuddyStore(db)
    _catalog_row(db, "groq", "llama-3.3-70b-versatile", name="Llama 3.3")
    _catalog_row(db, "groq", "whisper-large-v3", name="Whisper")
    _catalog_row(db, "groq", "gpt-oss-20b", free=False)
    _catalog_row(db, "google", "gemini-2.0-flash", name="Gemini Flash")

    source = DirectSource(settings, store)
    ids = {(item["provider"], item["model_id"]) for item in source.candidates()}
    assert ("groq", "llama-3.3-70b-versatile") in ids
    assert ("google", "gemini-2.0-flash") in ids
    # 音频模型不进文本池；付费模型不作为可调用资源
    assert ("groq", "whisper-large-v3") not in ids
    assert ("groq", "gpt-oss-20b") not in ids
    assert source.is_paid("groq", "gpt-oss-20b") is True
    # WorkBuddy 是桥接来源，不在这里当直连厂商
    assert not source.known("workbuddy")


def test_chat_url_and_auth_follow_chat_overrides(tmp_path):
    settings = _settings(tmp_path)
    source = DirectSource(settings, _store(tmp_path))
    groq = source.config("groq")
    google = source.config("google")
    assert source.chat_url(groq) == "https://api.groq.com/openai/v1/chat/completions"
    assert source.chat_url(google) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    headers, params = source._request_parts(google, "gem-test", stream=True)
    assert headers["authorization"] == "Bearer gem-test"
    assert params == {}
    headers, params = source._request_parts(groq, "gsk-test", stream=False)
    assert headers["authorization"] == "Bearer gsk-test"


async def test_post_and_stream_chat_use_openai_wire(tmp_path):
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if json.loads(request.content).get("stream") is True:
            return httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    source = DirectSource(settings, store, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        response = await source.post_chat(
            "groq",
            {
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": "hi"}],
                # Pool-only metadata must not leak to the provider.
                "session_id": "s1",
                "metadata": {"session_id": "s1"},
            },
        )
        assert response.json()["choices"][0]["message"]["content"] == "hello"
        assert seen[0].url == "https://api.groq.com/openai/v1/chat/completions"
        assert seen[0].headers["authorization"] == "Bearer gsk-test"
        forwarded = json.loads(seen[0].content)
        assert forwarded["model"] == "llama-3.3-70b-versatile"
        assert "session_id" not in forwarded and "metadata" not in forwarded

        stream = await source.open_chat(
            "groq", {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": "hi"}]}
        )
        body = b"".join([chunk async for chunk in stream.aiter_bytes()])
        await stream.aclose()
        assert b"data:" in body
        assert json.loads(seen[1].content)["stream"] is True
    finally:
        await source.aclose()


async def test_post_chat_maps_http_errors(tmp_path):
    settings = _settings(tmp_path)
    source = DirectSource(
        settings,
        _store(tmp_path),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(429, text="slow down"))
        ),
    )
    try:
        with pytest.raises(DirectError) as caught:
            await source.post_chat("groq", {"model": "m", "messages": []})
        assert caught.value.status_code == 429
    finally:
        await source.aclose()


def test_router_uses_direct_sources_and_binds_sessions(tmp_path):
    settings = _settings(tmp_path)
    db = Database(tmp_path / "direct.db")
    store = WorkBuddyStore(db)
    _catalog_row(db, "groq", "llama-3.3-70b-versatile")
    _catalog_row(db, "google", "gemini-2.0-flash")
    catalog = WorkBuddyCatalog(store, None)
    direct = DirectSource(settings, store)
    router = PoolRouter(store, catalog, direct=direct)

    intent = extract_intent({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    attempts = router.plan(intent, {}, session_id="s1")
    assert attempts and attempts[0].source == "direct"
    assert attempts[0].account is None
    binding = store.get_session("s1")
    assert binding is not None
    assert binding["account_id"] is None
    bound = (binding["provider"], binding["model_id"])

    # 再规划同一会话：绑定的模型排在最前（全局会话亲和）
    again = router.plan(intent, {}, session_id="s1")
    assert (again[0].provider, again[0].model_id) == bound

    explicit = router.plan(
        extract_intent(
            {"model": "groq/llama-3.3-70b-versatile", "messages": [{"role": "user", "content": "hi"}]}
        ),
        {},
    )
    assert explicit[0].provider == "groq"
    assert explicit[0].model_id == "llama-3.3-70b-versatile"


def test_router_keeps_diffusion_models_out_of_tool_requests(tmp_path):
    settings = _settings(tmp_path)
    db = Database(tmp_path / "direct.db")
    store = WorkBuddyStore(db)
    _catalog_row(db, "groq", "diffusiongemma-26b-a4b-it")
    _catalog_row(db, "groq", "glm-5.3-flash")
    router = PoolRouter(store, WorkBuddyCatalog(store, None), direct=DirectSource(settings, store))
    tools = [{"type": "function", "function": {"name": "ping", "parameters": {}}}]

    tool_plan = router.plan(
        extract_intent({"model": "auto", "tools": tools, "messages": [{"role": "user", "content": "hi"}]}),
        {},
    )
    assert tool_plan
    assert all("diffusion" not in item.model_id for item in tool_plan)

    plain_plan = router.plan(
        extract_intent({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}),
        {},
    )
    assert any("diffusion" in item.model_id for item in plain_plan)


def test_router_rejects_paid_and_non_text_explicit_models(tmp_path):
    settings = _settings(tmp_path)
    db = Database(tmp_path / "direct.db")
    store = WorkBuddyStore(db)
    _catalog_row(db, "groq", "llama-3.3-70b-versatile")
    _catalog_row(db, "groq", "gpt-oss-20b", free=False)
    _catalog_row(db, "groq", "whisper-large-v3")
    router = PoolRouter(store, WorkBuddyCatalog(store, None), direct=DirectSource(settings, store))

    with pytest.raises(PoolError) as paid:
        router.plan(extract_intent({"model": "groq/gpt-oss-20b", "messages": []}), {})
    assert paid.value.code == "model_not_free"

    with pytest.raises(PoolError) as non_text:
        router.plan(extract_intent({"model": "groq/whisper-large-v3", "messages": []}), {})
    assert non_text.value.code == "model_not_text"


def test_router_prefers_models_with_recent_successes(tmp_path):
    settings = _settings(tmp_path)
    db = Database(tmp_path / "direct.db")
    store = WorkBuddyStore(db)
    _catalog_row(db, "groq", "aaa-model")
    _catalog_row(db, "groq", "zzz-model")
    now = "2026-10-30T00:00:00+00:00"
    store.conn.execute(
        "INSERT INTO pool_request_attempts (request_id, attempt_no, account_id, model_id,"
        " provider, status_code, error_code, started_at, duration_ms, credits)"
        " VALUES ('r-proven', 1, NULL, 'zzz-model', 'groq', 200, NULL, ?, 10, 0)",
        (now,),
    )
    store.conn.commit()
    router = PoolRouter(store, WorkBuddyCatalog(store, None), direct=DirectSource(settings, store))
    attempts = router.plan(extract_intent({"model": "auto", "messages": []}), {})
    assert attempts[0].model_id == "zzz-model"


def test_router_no_sources_reports_no_accounts(tmp_path):
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    router = PoolRouter(store, WorkBuddyCatalog(store, None), direct=DirectSource(settings, store))
    with pytest.raises(PoolError) as caught:
        router.plan(extract_intent({"model": "auto", "messages": []}), {})
    assert caught.value.code == "no_accounts"


def test_unified_inventory_marks_callable_and_catalog_free_filters_non_text(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        service = app.state.workbuddy
        now = "2026-10-30T00:00:00+00:00"
        for provider, model_id, free in (
            ("groq", "llama-3.3-70b-versatile", True),
            ("groq", "gpt-oss-20b", False),
            ("groq", "whisper-large-v3", True),
            ("google", "gemini-2.0-flash", True),
        ):
            service.store.conn.execute(
                "INSERT INTO models (provider_id, model_id, name, context_length, max_output,"
                " is_free, free_kind, fingerprint, first_seen, last_seen, present)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (provider, model_id, model_id, 32_000, 4096, 1 if free else 0,
                 "free_tier" if free else "paid", model_id, now, now),
            )
        service.store.conn.commit()

        admin = {"Authorization": "Bearer test-admin-token"}
        inventory = client.get("/api/models", headers=admin).json()
        by_id = {item["wire_id"]: item for item in inventory["items"]}
        assert by_id["groq/llama-3.3-70b-versatile"]["callable"] is True
        assert by_id["google/gemini-2.0-flash"]["callable"] is True
        paid = by_id["groq/gpt-oss-20b"]
        assert paid["callable"] is False and paid["reason"] == "paid"
        # 非文本模型不进入统一清单
        assert "groq/whisper-large-v3" not in by_id
        assert inventory["summary"]["callable"] >= 2

        free = client.get("/api/catalog/free", headers=admin).json()
        free_ids = {item["wire_id"] for item in free["items"]}
        assert "groq/llama-3.3-70b-versatile" in free_ids
        assert "groq/gpt-oss-20b" not in free_ids
        assert "groq/whisper-large-v3" not in free_ids


def test_auto_retries_model_404_and_cools_the_model(tmp_path, monkeypatch):
    """A catalog entry the provider cannot serve must not sink the whole request."""
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        service = app.state.workbuddy
        now = "2026-10-30T00:00:00+00:00"
        for model_id in ("aaa-bad", "zzz-good"):
            service.store.conn.execute(
                "INSERT INTO models (provider_id, model_id, name, context_length, max_output,"
                " is_free, free_kind, fingerprint, first_seen, last_seen, present)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                ("groq", model_id, model_id, 32_000, 4096, 1, "free_tier", model_id, now, now),
            )
        service.store.conn.commit()
        # Deterministic order: cost mode sorts by model_id.
        service.store.set_setting("policy", {"routing_mode": "cost"})

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body["model"])
            if body["model"] == "aaa-bad":
                return httpx.Response(
                    404,
                    json={
                        "status": 404,
                        "title": "Not Found",
                        "detail": "Function 'x': Not found for account 'y'",
                    },
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}},
            )

        service.direct.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created = client.post(
            "/api/keys",
            headers={"Authorization": "Bearer test-admin-token"},
            json={"name": "smoke", "kind": "shared"},
        )
        auth = {"Authorization": f"Bearer {created.json()['key']}"}

        first = client.post(
            "/v1/chat/completions",
            headers={**auth, "X-Session-ID": "s1"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert first.status_code == 200
        assert first.headers["X-Pool-Model"] == "zzz-good"
        assert calls[:2] == ["aaa-bad", "zzz-good"]

        # The failed model is cooling, so the next request skips it entirely.
        calls.clear()
        second = client.post(
            "/v1/chat/completions",
            headers={**auth, "X-Session-ID": "s2"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert second.status_code == 200
        assert calls == ["zzz-good"]

        # A session still bound to the bad model is re-planned and rebound.
        service.store.upsert_session(
            "s-bad", provider="groq", model_id="aaa-bad", account_id=None, ttl_seconds=600
        )
        calls.clear()
        third = client.post(
            "/v1/chat/completions",
            headers={**auth, "X-Session-ID": "s-bad"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert third.status_code == 200
        assert calls[0] == "zzz-good"
        binding = service.store.get_session("s-bad")
        assert binding is not None and binding["model_id"] == "zzz-good"


def test_unified_v1_serves_direct_model_with_session_affinity(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        service = app.state.workbuddy
        # seed the provider catalog through the store connection
        for model_id in ("llama-3.3-70b-versatile", "gemini-2.0-flash"):
            provider = "groq" if model_id.startswith("llama") else "google"
            now = "2026-10-30T00:00:00+00:00"
            service.store.conn.execute(
                "INSERT INTO models (provider_id, model_id, name, context_length, max_output,"
                " is_free, free_kind, fingerprint, first_seen, last_seen, present)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (provider, model_id, model_id, 32_000, 4096, 1, "free_tier", model_id, now, now),
            )
        service.store.conn.commit()

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "answer from " + body["model"]}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                },
            )

        service.direct.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created = client.post(
            "/api/keys",
            headers={"Authorization": "Bearer test-admin-token"},
            json={"name": "smoke", "kind": "shared"},
        )
        pool_key = created.json()["key"]
        auth = {"Authorization": f"Bearer {pool_key}"}

        models = client.get("/v1/models", headers=auth)
        ids = [item["id"] for item in models.json()["data"]]
        assert "groq/llama-3.3-70b-versatile" in ids
        assert "google/gemini-2.0-flash" in ids

        response = client.post(
            "/v1/chat/completions",
            headers={**auth, "X-Session-ID": "global-session"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.headers["X-Pool-Source"] == "direct"
        assert response.headers["X-Pool-Free"] == "1"
        assert response.headers.get("X-Pool-Account", "") == ""
        assert seen and seen[0].headers["authorization"] in {"Bearer gsk-test", "Bearer gem-test"}

        binding = service.store.get_session("global-session")
        assert binding is not None and binding["account_id"] is None
        bound_id = binding["model_id"]

        # Same session -> the bound model is tried first (recorded model matches)
        client.post(
            "/v1/chat/completions",
            headers={**auth, "X-Session-ID": "global-session"},
            json={"model": "auto", "messages": [{"role": "user", "content": "again"}]},
        )
        assert json.loads(seen[-1].content)["model"] == bound_id

        requests = service.store.list_requests()
        assert requests
        latest = requests[0]
        assert latest["provider"] in {"groq", "google"}
        assert latest["cost_source"] == "free"
        assert latest["credits"] == 0.0
        assert latest["bridge_account_id"] is None
