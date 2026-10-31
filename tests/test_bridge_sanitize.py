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
