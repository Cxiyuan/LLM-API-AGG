"""Console-entered provider keys: encrypted at rest, never returned, env fallback."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import load_settings
from catalogwatch.poller import PollResult

pytest.importorskip("cryptography")


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
    return load_settings(config, db_path=tmp_path / "keys.db")


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CATALOGWATCH_PROVIDER_KEY_SECRET", "test-provider-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return create_app(_settings(tmp_path))


def test_key_is_encrypted_and_listing_never_returns_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    admin = {"Authorization": "Bearer test-admin-token"}
    with TestClient(app) as client:
        listing = client.get("/api/providers", headers=admin).json()
        groq = next(item for item in listing["items"] if item["id"] == "groq")
        assert groq["configured"] is False
        assert groq["source"] is None

        saved = client.put(
            "/api/providers/groq/key", headers=admin, json={"api_key": "gsk-secret-value"}
        )
        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert saved.json()["source"] == "console"

        row = app.state.service.db.conn.execute(
            "SELECT ciphertext FROM provider_keys WHERE provider_id='groq'"
        ).fetchone()
        assert row is not None
        assert "gsk-secret-value" not in row["ciphertext"]

        text = client.get("/api/providers", headers=admin).text
        assert "gsk-secret-value" not in text
        assert app.state.workbuddy.direct.key("groq") == "gsk-secret-value"

        cleared = client.delete("/api/providers/groq/key", headers=admin)
        assert cleared.status_code == 200
        assert cleared.json()["configured"] is False
        assert app.state.workbuddy.direct.key("groq") is None


def test_console_key_takes_precedence_over_environment(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    admin = {"Authorization": "Bearer test-admin-token"}
    with TestClient(app) as client:
        monkeypatch.setenv("GROQ_API_KEY", "env-value")
        assert app.state.workbuddy.direct.key("groq") == "env-value"
        client.put("/api/providers/groq/key", headers=admin, json={"api_key": "console-value"})
        assert app.state.workbuddy.direct.key("groq") == "console-value"
        client.delete("/api/providers/groq/key", headers=admin)
        assert app.state.workbuddy.direct.key("groq") == "env-value"


def test_key_validation_and_bridge_rejection(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    admin = {"Authorization": "Bearer test-admin-token"}
    with TestClient(app) as client:
        assert client.put(
            "/api/providers/groq/key", headers=admin, json={"api_key": "   "}
        ).status_code == 400
        assert client.put(
            "/api/providers/nope/key", headers=admin, json={"api_key": "x"}
        ).status_code == 404
        assert client.put(
            "/api/providers/workbuddy/key", headers=admin, json={"api_key": "x"}
        ).status_code == 400


def test_poll_endpoint_reports_catalog_result(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    admin = {"Authorization": "Bearer test-admin-token"}

    async def fake_poll(provider_id: str, notify: bool = True):
        return PollResult(provider_id, "ok", model_count=3, free_model_count=2), []

    app.state.service.poll_provider = fake_poll
    with TestClient(app) as client:
        response = client.post("/api/providers/groq/poll", headers=admin)
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["model_count"] == 3
        assert body["free_model_count"] == 2
