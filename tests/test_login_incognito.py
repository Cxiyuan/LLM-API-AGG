from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_oauth():
    # The vendored bridge expects these sibling modules on sys.path.
    auth_manager = sys.modules.setdefault(
        "auth_manager", types.ModuleType("auth_manager")
    )
    auth_manager.backend_url = lambda: "https://www.codebuddy.cn"  # type: ignore[attr-defined]
    sys.modules.setdefault("fingerprint", types.ModuleType("fingerprint"))
    path = ROOT / "vendor" / "buddy2api" / "providers" / "workbuddy" / "oauth.py"
    spec = importlib.util.spec_from_file_location("workbuddy_oauth_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_global_login_requires_incognito_and_keeps_force_login():
    oauth = _load_oauth()

    async def fake_exchange(method, url, origin, bearer=""):
        return 200, {
            "code": 0,
            "data": {
                "state": "abcdefgh",
                "authUrl": "https://www.workbuddy.ai/login?platform=CLI&state=abcdefgh",
            },
        }

    oauth._exchange = fake_exchange  # type: ignore[assignment]
    result = asyncio.run(oauth.start_login("global"))
    assert result["requires_incognito"] is True
    assert "force_login=1" in result["url"]
    assert "无痕" in result["note"]

    cn = asyncio.run(oauth.start_login("cn"))
    assert cn["requires_incognito"] is False


def test_new_login_uses_the_provider_of_its_domain():
    import importlib.util as importlib_util
    import types

    captured = {}
    db_stub = types.ModuleType("database")
    db_stub.list_accounts = lambda provider=None: []
    db_stub.add_account = lambda data: captured.update(data) or 4
    sys.modules["database"] = db_stub
    fingerprint_stub = sys.modules.setdefault(
        "fingerprint", types.ModuleType("fingerprint")
    )
    fingerprint_stub.user_agent = lambda: "test-agent"  # type: ignore[attr-defined]

    path = ROOT / "vendor" / "buddy2api" / "auth_manager.py"
    spec = importlib_util.spec_from_file_location("buddy2api_auth_test", path)
    assert spec and spec.loader
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.upsert_login_account(
        {"uid": "intl-user", "nickname": "intl", "domain": "www.workbuddy.ai"}
    )
    assert result["id"] == 4
    assert captured["provider"] == "workbuddy-i"

    captured.clear()
    module.upsert_login_account(
        {"uid": "cn-user", "nickname": "cn", "domain": "www.codebuddy.cn"}
    )
    assert captured["provider"] == "workbuddy"
