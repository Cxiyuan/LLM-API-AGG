from __future__ import annotations

from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import Settings


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")
    return TestClient(create_app(Settings(db_path=tmp_path / "api.db")))


def test_health_and_removed_catalog_routes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        body = health.json()
        assert body["ok"] is True
        assert "accounts" in body["stats"]
        assert "models" in body["stats"]
        assert "workbuddy" in body

        for path in (
            "/stats",
            "/providers",
            "/models",
            "/catalog",
            "/changes",
            "/quota/sources",
        ):
            assert client.get(path).status_code == 404, path
        assert client.post("/poll").status_code == 404


def test_admin_auth_and_console(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/overview").status_code == 401
        response = client.get(
            "/api/overview",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert "overview" in payload
        assert "bridge" in payload

        page = client.get("/", follow_redirects=False)
        assert page.status_code == 307
        assert page.headers["location"].rstrip("/") == "http://testserver"


def test_removed_catalog_modules_are_gone():
    import importlib.util

    for name in (
        "catalogwatch.poller",
        "catalogwatch.quota",
        "catalogwatch.extract",
        "catalogwatch.notify",
        "catalogwatch.models",
        "catalogwatch.keystore",
        "catalogwatch.service",
        "catalogwatch.adapters",
        "catalogwatch.workbuddy.direct",
    ):
        assert importlib.util.find_spec(name) is None, name
