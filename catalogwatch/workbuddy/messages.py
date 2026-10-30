"""Minimal Anthropic Messages <-> OpenAI Chat Completions adapter.

Buddy2api exposes Chat Completions; the pool wants to also serve Claude Code
and other Anthropic Messages clients. This module translates the common
request/response shapes and the streaming event sequence. It deliberately
supports the subset used by coding agents: text, images, tools, tool results,
stop sequences, and streaming text/tool-call deltas.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def map_model(value: Any) -> str:
    model = str(value or "auto").strip() or "auto"
    if model in {"auto", "free", "cheap"}:
        return model
    # WorkBuddy does not serve Anthropic model names directly; let the pool
    # choose a WorkBuddy model for Claude-branded clients.
    if model.lower().startswith("claude"):
        return "auto"
    return model


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _image_part(block: dict) -> dict | None:
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("type") == "base64":
        media = str(source.get("media_type") or "image/png")
        data = str(source.get("data") or "")
        if not data:
            return None
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media};base64,{data}"},
        }
    if source.get("type") == "url":
        url = str(source.get("url") or "")
        if url:
            return {"type": "image_url", "image_url": {"url": url}}
    return None


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(content)
    if content is None:
        return ""
    return str(content)


def _user_blocks(content: list[dict]) -> list[dict]:
    """Translate one Anthropic user turn into OpenAI messages."""
    texts: list[str] = []
    parts: list[dict] = []
    tool_messages: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif kind == "image":
            image = _image_part(block)
            if image:
                parts.append(image)
        elif kind == "tool_result":
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": _tool_result_text(block.get("content")),
                }
            )
    messages: list[dict] = list(tool_messages)
    if parts:
        content_parts: list[dict] = []
        if texts:
            content_parts.append({"type": "text", "text": "\n".join(texts)})
        content_parts.extend(parts)
        messages.append({"role": "user", "content": content_parts})
    elif texts:
        messages.append({"role": "user", "content": "\n".join(texts)})
    return messages


def _assistant_blocks(content: list[dict]) -> list[dict]:
    texts: list[str] = []
    tool_calls: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif kind == "tool_use":
            tool_input = block.get("input")
            try:
                arguments = json.dumps(tool_input if tool_input is not None else {}, ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = "{}"
            tool_calls.append(
                {
                    "id": str(block.get("id") or _new_id("call_")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": arguments,
                    },
                }
            )
    if not texts and not tool_calls:
        return []
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return [message]


def _tool_choice(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and value.get("name"):
        return {"type": "function", "function": {"name": str(value["name"])}}
    return None


def to_openai_request(payload: dict) -> dict:
    messages: list[dict] = []
    system = _system_text(payload.get("system"))
    if system:
        messages.append({"role": "system", "content": system})

    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            if role == "assistant":
                messages.extend(_assistant_blocks(content))
            else:
                messages.extend(_user_blocks(content))

    out: dict[str, Any] = {
        "model": map_model(payload.get("model")),
        "messages": messages,
    }
    for source, target in (
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
    ):
        if payload.get(source) is not None:
            out[target] = payload[source]
    if payload.get("stop_sequences"):
        out["stop"] = payload["stop_sequences"]

    tools = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    if tools:
        out["tools"] = tools
        choice = _tool_choice(payload.get("tool_choice"))
        if choice is not None:
            out["tool_choice"] = choice

    if payload.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def _stop_reason(finish_reason: Any, has_tools: bool) -> str:
    if has_tools:
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "tool_calls":
        return "tool_use"
    return "end_turn"


def to_anthropic_response(body: dict, request_model: str) -> dict:
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content: list[dict] = []
    if message.get("content"):
        content.append({"type": "text", "text": str(message["content"])})
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        raw = function.get("arguments")
        try:
            tool_input = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        except (ValueError, TypeError):
            tool_input = {"raw": raw}
        if not isinstance(tool_input, dict):
            tool_input = {"value": tool_input}
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or _new_id("toolu_")),
                "name": str(function.get("name") or ""),
                "input": tool_input,
            }
        )
    if not content:
        content.append({"type": "text", "text": ""})
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return {
        "id": str(body.get("id") or _new_id("msg_")),
        "type": "message",
        "role": "assistant",
        "model": str(body.get("model") or request_model),
        "content": content,
        "stop_reason": _stop_reason(choice.get("finish_reason"), bool(tool_calls)),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def anthropic_error(status: int, message: str) -> dict:
    if status in {401, 403}:
        kind = "authentication_error"
    elif status == 429:
        kind = "rate_limit_error"
    elif status == 400:
        kind = "invalid_request_error"
    else:
        kind = "api_error"
    return {"type": "error", "error": {"type": kind, "message": str(message)[:500]}}


def _sse(event: str, data: dict) -> bytes:
    return (
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


async def translate_stream(
    chunks: AsyncIterator[bytes], request_model: str
) -> AsyncIterator[bytes]:
    """Translate OpenAI Chat SSE chunks into Anthropic Messages SSE events."""
    message_id = _new_id("msg_")
    model = request_model or "auto"
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    next_index = 0
    text_index: int | None = None
    tool_states: dict[int, dict] = {}
    opened: list[int] = []
    finish_reason = None
    usage: dict = {}
    buffer = b""
    saw_error = False

    def start_tool(state: dict) -> list[bytes]:
        nonlocal next_index
        block_index = next_index
        next_index += 1
        state["block_index"] = block_index
        state["started"] = True
        opened.append(block_index)
        events = [
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": state.get("id") or _new_id("toolu_"),
                        "name": state.get("name") or "",
                        "input": {},
                    },
                },
            )
        ]
        pending = state.get("pending_args") or ""
        if pending:
            events.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "input_json_delta", "partial_json": pending},
                    },
                )
            )
            state["pending_args"] = ""
        return events

    async for chunk in chunks:
        buffer += chunk
        if len(buffer) > 256_000:
            buffer = buffer[-128_000:]
        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            line = raw_line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                value = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            if isinstance(value.get("error"), dict):
                saw_error = True
                yield _sse(
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": str(value["error"].get("message") or "upstream error"),
                        },
                    },
                )
                break
            if isinstance(value.get("usage"), dict):
                usage = value["usage"]
            choices = value.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(delta.get("content"), str) and delta["content"]:
                if text_index is None:
                    text_index = next_index
                    next_index += 1
                    opened.append(text_index)
                    yield _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": text_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": text_index,
                        "delta": {"type": "text_delta", "text": delta["content"]},
                    },
                )
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    try:
                        tool_index = int(call.get("index") or 0)
                    except (TypeError, ValueError):
                        tool_index = 0
                    state = tool_states.setdefault(
                        tool_index,
                        {
                            "id": None,
                            "name": "",
                            "pending_args": "",
                            "started": False,
                            "block_index": None,
                        },
                    )
                    if call.get("id"):
                        state["id"] = str(call["id"])
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    if function.get("name"):
                        state["name"] = str(function["name"])
                    args = function.get("arguments")
                    if isinstance(args, str) and args:
                        if not state["started"]:
                            state["pending_args"] += args
                        else:
                            yield _sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": state["block_index"],
                                    "delta": {"type": "input_json_delta", "partial_json": args},
                                },
                            )
                    if not state["started"] and state["name"]:
                        for event in start_tool(state):
                            yield event
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
        if saw_error:
            return

    # Tool calls that only received a name at the end still need a block.
    for state in tool_states.values():
        if not state["started"] and state["name"]:
            for event in start_tool(state):
                yield event

    if text_index is None and not any(state["started"] for state in tool_states.values()):
        text_index = next_index
        opened.append(text_index)
        yield _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": text_index,
                "content_block": {"type": "text", "text": ""},
            },
        )

    for index in sorted(opened):
        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": index},
        )

    has_tools = any(state["started"] for state in tool_states.values())
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": _stop_reason(finish_reason, has_tools),
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})
