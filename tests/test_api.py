from __future__ import annotations

from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import load_settings


def test_api_health_and_empty_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
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
    settings = load_settings(config, db_path=tmp_path / "api.db")

    with TestClient(create_app(settings)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        catalog = client.get("/catalog")
        assert catalog.status_code == 200
        assert catalog.json()["providers"] == []

        providers = client.get("/providers").json()["items"]
        assert providers[0]["id"] == "demo"

        changes = client.get("/changes?since=24h")
        assert changes.status_code == 200
        assert changes.json()["items"] == []


def test_api_rejects_bad_since(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  - id: demo
    base_url: https://example.com/v1
    key_required: false
""",
        encoding="utf-8",
    )
    settings = load_settings(config, db_path=tmp_path / "api.db")

    with TestClient(create_app(settings)) as client:
        response = client.get("/changes?since=whenever")
        assert response.status_code == 400
