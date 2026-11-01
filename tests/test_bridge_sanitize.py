from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _outbound():
    path = ROOT / "vendor" / "buddy2api" / "workbuddy_outbound.py"
    spec = importlib.util.spec_from_file_location("buddy2api_outbound", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deepseek_harness_mentions_are_sanitized():
    module = _outbound()
    body = module.prepare(
        {
            "model": "workbuddy/glm-5.3-flash",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an AI agent powered by DeepSeek Harness.\n\n"
                        "Current DeepSeek Harness implementation checkout: /opt/agent/dsh/."
                    ),
                },
                {"role": "user", "content": "hi"},
            ],
        }
    )
    text = body["messages"][0]["content"]
    assert "DeepSeek Harness" not in text
    assert "the harness" in text


def test_unrelated_user_text_is_not_rewritten():
    module = _outbound()
    body = module.prepare(
        {
            "model": "workbuddy/glm-5.3-flash",
            "messages": [{"role": "user", "content": "hello world"}],
        }
    )
    assert body["messages"][0]["content"] == "hello world"


def test_model_quota_rate_codes_do_not_cool_the_account():
    module = _outbound()
    for code in (6004, 14003):
        action, status, _detail, failure = module.account_rule(
            429, {"code": code, "msg": "model busy or quota"}
        )
        assert action == "stop"
        assert status == 429
        assert failure == "upstream_http"
    # A rotation code that is not model-scoped still rotates accounts.
    action, *_ = module.account_rule(429, {"code": 6001, "msg": "account rate"})
    assert action == "rotate"
