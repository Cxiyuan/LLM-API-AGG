from __future__ import annotations

import json

from catalogwatch.workbuddy.messages import (
    map_model,
    to_anthropic_response,
    to_openai_request,
    translate_stream,
)


def test_map_model_claude_to_auto():
    assert map_model("claude-sonnet-4-20250514") == "auto"
    assert map_model("auto") == "auto"
    assert map_model("workbuddy/glm-5.2") == "workbuddy/glm-5.2"


def test_request_translation_text_image_tools_and_tool_result():
    payload = {
        "model": "claude-sonnet-4",
        "max_tokens": 128,
        "system": "be brief",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "a.txt"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
                ],
            },
        ],
        "tools": [
            {
                "name": "read_file",
                "description": "read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        "tool_choice": {"type": "auto"},
        "stop_sequences": ["END"],
    }
    out = to_openai_request(payload)
    assert out["model"] == "auto"
    assert out["max_tokens"] == 128
    assert out["stop"] == ["END"]
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    user = out["messages"][1]
    assert user["role"] == "user"
    assert user["content"][0]["type"] == "text"
    assert user["content"][1]["type"] == "image_url"
    assert user["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assistant = next(item for item in out["messages"] if item.get("tool_calls"))
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    assert any(item.get("role") == "tool" and item.get("tool_call_id") == "toolu_1" for item in out["messages"])
    assert out["tools"][0]["function"]["parameters"]["type"] == "object"
    assert out["tool_choice"] == "auto"


def test_response_translation_text_and_tool_use():
    body = {
        "id": "chatcmpl-1",
        "model": "workbuddy/free-model",
        "choices": [
            {
                "message": {
                    "content": "hello",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "a.txt"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    out = to_anthropic_response(body, "claude-sonnet-4")
    assert out["type"] == "message"
    assert out["stop_reason"] == "tool_use"
    assert out["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert out["content"][0] == {"type": "text", "text": "hello"}
    assert out["content"][1]["type"] == "tool_use"
    assert out["content"][1]["input"] == {"path": "a.txt"}


async def test_stream_translation_events():
    async def chunks():
        yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        yield (
            b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    text = b"".join([event async for event in translate_stream(chunks(), "claude-sonnet-4")]).decode()
    assert "event: message_start" in text
    assert "content_block_start" in text
    assert '"type": "text_delta"' in text
    assert '"text": "hel"' in text
    assert '"text": "lo"' in text
    assert "content_block_stop" in text
    assert "message_delta" in text
    assert "message_stop" in text
    assert '"stop_reason": "end_turn"' in text
    assert '"output_tokens": 2' in text


async def test_stream_translation_tool_calls():
    async def chunks():
        yield (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            b'"type":"function","function":{"name":"read_file","arguments":"{\\"pa"}}]}}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            b'"function":{"arguments":"th\\":\\"a.txt\\"}"}}]},"finish_reason":"tool_calls"}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    text = b"".join([event async for event in translate_stream(chunks(), "claude")]).decode()
    assert '"type": "tool_use"' in text
    assert '"name": "read_file"' in text
    assert "input_json_delta" in text
    assert "content_block_stop" in text
    assert '"stop_reason": "tool_use"' in text
