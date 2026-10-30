from __future__ import annotations

from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import load_settings


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


def test_console_auth_and_empty_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/").status_code == 200
        assert "WorkBuddy Pool" in client.get("/").text

        unauthorized = client.get("/api/overview")
        assert unauthorized.status_code == 401

        bad = client.post("/api/auth/login", json={"token": "wrong"})
        assert bad.status_code == 401

        login = client.post("/api/auth/login", json={"token": "test-admin-token"})
        assert login.status_code == 200
        assert client.cookies.get("cw_admin")
        assert client.cookies.get("cw_csrf")

        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json()["actor"] == "session"

        accounts = client.get("/api/workbuddy/accounts")
        assert accounts.status_code == 200
        assert accounts.json()["items"] == []

        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["ok"] is True

        # Cookie-authenticated mutations require the double-submit CSRF token.
        no_csrf = client.post("/api/workbuddy/catalog/refresh")
        assert no_csrf.status_code == 403

        bearer = client.get(
            "/api/auth/session",
            headers={"Authorization": "Bearer test-admin-token"},
            cookies={},
        )
        assert bearer.status_code == 200
        assert bearer.json()["actor"] == "bearer"
