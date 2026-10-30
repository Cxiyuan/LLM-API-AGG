from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_bootstrap():
    path = ROOT / "deploy" / "start_catalogwatch.py"
    spec = importlib.util.spec_from_file_location("container_bootstrap", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workbuddy_provider_is_opt_in():
    data = yaml.safe_load((ROOT / "config" / "providers.yaml").read_text(encoding="utf-8"))
    provider = next(item for item in data["providers"] if item["id"] == "workbuddy")
    assert provider["enabled"] is False
    assert provider["adapter"] == "workbuddy"
    assert provider["key_env"] == "WORKBUDDY_GATEWAY_KEY"
    assert provider["base_url"] == "http://127.0.0.1:8787/v1"


def test_container_config_enables_workbuddy(tmp_path):
    module = _load_bootstrap()
    base = tmp_path / "base.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "id": "workbuddy",
                        "enabled": False,
                        "adapter": "workbuddy",
                        "base_url": "http://old/v1",
                        "key_env": "OLD_KEY",
                    },
                    {"id": "other", "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.yaml"

    module.write_container_config(base, out, "http://127.0.0.1:8787")
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    workbuddy = next(item for item in data["providers"] if item["id"] == "workbuddy")
    other = next(item for item in data["providers"] if item["id"] == "other")

    assert workbuddy["enabled"] is True
    assert workbuddy["base_url"] == "http://127.0.0.1:8787/v1"
    assert workbuddy["key_env"] == "WORKBUDDY_GATEWAY_KEY"
    assert workbuddy["key_required"] is True
    assert other["enabled"] is False


def test_gateway_key_is_provisioned_and_reused(tmp_path):
    module = _load_bootstrap()
    key_file = tmp_path / "secrets" / "gateway_key"
    posts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            auth = request.headers.get("authorization", "")
            if auth == "Bearer sk-cb-test":
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(401, json={"detail": "Invalid API key"})
        if request.url.path == "/admin/api-keys" and request.method == "POST":
            posts["count"] += 1
            assert request.headers.get("authorization") == "Bearer admin-token"
            body = json.loads(request.content)
            assert body["default_channel"] == "workbuddy"
            return httpx.Response(200, json={"id": 1, "key": "sk-cb-test"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    key = module.ensure_gateway_key(
        "http://bridge", "admin-token", key_file, transport=transport
    )

    assert key == "sk-cb-test"
    assert key_file.read_text(encoding="utf-8").strip() == "sk-cb-test"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert posts["count"] == 1

    # A second start reuses the existing, valid key instead of minting another.
    again = module.ensure_gateway_key(
        "http://bridge", "admin-token", key_file, transport=transport
    )
    assert again == key
    assert posts["count"] == 1


def test_gateway_key_is_replaced_when_the_database_was_reset(tmp_path):
    module = _load_bootstrap()
    key_file = tmp_path / "gateway_key"
    key_file.write_text("sk-cb-stale\n", encoding="utf-8")
    posts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            auth = request.headers.get("authorization", "")
            return (
                httpx.Response(200, json={"data": []})
                if auth == "Bearer sk-cb-new"
                else httpx.Response(401, json={"detail": "Invalid API key"})
            )
        if request.url.path == "/admin/api-keys":
            posts["count"] += 1
            return httpx.Response(200, json={"id": 2, "key": "sk-cb-new"})
        return httpx.Response(404)

    key = module.ensure_gateway_key(
        "http://bridge",
        "admin-token",
        key_file,
        transport=httpx.MockTransport(handler),
    )
    assert key == "sk-cb-new"
    assert posts["count"] == 1
    assert key_file.read_text(encoding="utf-8").strip() == "sk-cb-new"


def test_vendored_bridge_is_pinned():
    upstream = (ROOT / "vendor" / "buddy2api" / "UPSTREAM.md").read_text(encoding="utf-8")
    assert "16a18af04fde8cd22c3e69ccfb1cce9045ac0ac1" in upstream
    assert (ROOT / "vendor" / "buddy2api" / "server.py").exists()
    assert (ROOT / "vendor" / "buddy2api" / "LICENSE").exists()
    assert (ROOT / "vendor" / "buddy2api" / "providers" / "workbuddy" / "oauth.py").exists()
