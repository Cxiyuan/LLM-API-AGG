"""OpenAI-compatible pool data plane (`/v1/*`)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..util import utcnow_iso
from .auth import require_pool_key
from .gateway import PoolHTTPError, error_body
from .messages import anthropic_error, to_anthropic_response, to_openai_request, translate_stream
from .router import PoolError
from .service import WorkBuddyService

router = APIRouter(prefix="/v1")


def _service(request: Request) -> WorkBuddyService:
    service = getattr(request.app.state, "workbuddy", None)
    if service is None:
        raise HTTPException(status_code=503, detail="WorkBuddy pool is not initialized")
    return service


async def _json_body(request: Request) -> dict:
    try:
        value = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="JSON body required") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return value


def _session_id(request: Request, payload: dict) -> str | None:
    for name in ("x-session-id", "x-session-Id", "x-claude-code-session-id"):
        value = request.headers.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("session_id", "sessionId"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return None


def _bind_session_for(model: object) -> bool:
    return str(model or "auto") in {"auto", "free", "cheap"}


def _error_response(error: Exception) -> JSONResponse:
    if isinstance(error, (PoolError, PoolHTTPError)):
        return JSONResponse(
            status_code=error.status_code,
            content=error_body(error.code, error.message if hasattr(error, "message") else str(error)),
        )
    return JSONResponse(
        status_code=502,
        content=error_body("pool_error", str(error)[:500]),
    )


@router.get("/models")
async def list_models(request: Request) -> dict:
    service = _service(request)
    key = require_pool_key(request, service.store)
    allowed = key.get("allowed_models")
    allowed_set = {str(item) for item in allowed} if isinstance(allowed, list) and allowed else None
    data = [
        {
            "id": "auto",
            "object": "model",
            "created": 0,
            "owned_by": "workbuddy-pool",
            "description": "Cost-aware pool selection: free now, then low multiplier; night promotions apply",
        },
        {
            "id": "free",
            "object": "model",
            "created": 0,
            "owned_by": "workbuddy-pool",
            "description": "Only models that are free at the current time",
        },
        {
            "id": "cheap",
            "object": "model",
            "created": 0,
            "owned_by": "workbuddy-pool",
            "description": "Lowest current effective multiplier",
        },
    ]
    resources = list(service.catalog.list_costs())
    direct = getattr(service, "direct", None)
    if direct is not None:
        resources.extend(direct.candidates())
    for item in resources:
        model_id = item["model_id"]
        pool_id = f"{item['provider']}/{model_id}"
        if allowed_set and model_id not in allowed_set and pool_id not in allowed_set:
            continue
        data.append(
            {
                "id": pool_id,
                "object": "model",
                "created": 0,
                "owned_by": f"llm-agg/{item['provider']}",
                "source": item.get("source") or "workbuddy",
                "provider": item["provider"],
                "context_length": item.get("context_length"),
                "max_output_tokens": item.get("max_output"),
                "capabilities": item.get("capabilities") or {},
                "base_credits": item.get("base_credits"),
                "credits": item.get("credits"),
                "free": bool(item.get("free")),
                "promotion": {
                    "active": bool(item.get("promotion_active")),
                    "factor": item.get("promotion_factor"),
                    "badge": item.get("badge"),
                    "until": item.get("promotion_until"),
                },
                "cost_source": item.get("cost_source"),
            }
        )
    return {"object": "list", "data": data, "updated_at": utcnow_iso()}


@router.post("/chat/completions")
async def chat_completions(request: Request):
    service = _service(request)
    key = require_pool_key(request, service.store)
    payload = await _json_body(request)
    session_id = _session_id(request, payload)
    if payload.get("stream"):
        try:
            (
                response,
                attempt,
                request_id,
                attempt_no,
                headers,
                started,
                key_info,
            ) = await service.gateway.open_stream(
                payload, key, endpoint="chat", session_id=session_id
            )
        except (PoolError, PoolHTTPError) as exc:
            return _error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return StreamingResponse(
            service.gateway.stream_body(
                response,
                attempt,
                request_id,
                attempt_no,
                key_info,
                started,
                session_id=session_id,
                bind_session=_bind_session_for(payload.get("model")),
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    try:
        status, body, headers = await service.gateway.chat(
            payload, key, endpoint="chat", session_id=session_id
        )
    except (PoolError, PoolHTTPError) as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)
    return JSONResponse(status_code=status, content=body, headers=headers)


@router.post("/responses")
async def responses(request: Request):
    service = _service(request)
    key = require_pool_key(request, service.store)
    payload = await _json_body(request)
    session_id = _session_id(request, payload)
    if payload.get("stream"):
        try:
            (
                response,
                attempt,
                request_id,
                attempt_no,
                headers,
                started,
                key_info,
            ) = await service.gateway.open_stream(
                payload, key, endpoint="responses", session_id=session_id
            )
        except (PoolError, PoolHTTPError) as exc:
            return _error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return StreamingResponse(
            service.gateway.stream_body(
                response,
                attempt,
                request_id,
                attempt_no,
                key_info,
                started,
                session_id=session_id,
                bind_session=_bind_session_for(payload.get("model")),
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    try:
        status, body, headers = await service.gateway.chat(
            payload, key, endpoint="responses", session_id=session_id
        )
    except (PoolError, PoolHTTPError) as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)
    return JSONResponse(status_code=status, content=body, headers=headers)


@router.post("/messages")
async def messages(request: Request):
    """Anthropic Messages compatibility for Claude Code and similar clients."""
    service = _service(request)
    key = require_pool_key(request, service.store)
    payload = await _json_body(request)
    if not isinstance(payload.get("messages"), list) or not payload.get("messages"):
        return JSONResponse(
            status_code=400,
            content=anthropic_error(400, "messages is required"),
        )
    if payload.get("max_tokens") is None:
        return JSONResponse(
            status_code=400,
            content=anthropic_error(400, "max_tokens is required"),
        )

    openai_payload = to_openai_request(payload)
    request_model = str(payload.get("model") or "auto")
    session_id = _session_id(request, payload)

    if payload.get("stream"):
        try:
            (
                response,
                attempt,
                request_id,
                attempt_no,
                headers,
                started,
                key_info,
            ) = await service.gateway.open_stream(
                openai_payload, key, endpoint="chat", session_id=session_id
            )
        except (PoolError, PoolHTTPError) as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=anthropic_error(exc.status_code, str(exc)),
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content=anthropic_error(502, str(exc)),
            )
        raw_stream = service.gateway.stream_body(
            response,
            attempt,
            request_id,
            attempt_no,
            key_info,
            started,
            session_id=session_id,
            bind_session=_bind_session_for(openai_payload.get("model")),
        )
        return StreamingResponse(
            translate_stream(raw_stream, request_model),
            media_type="text/event-stream",
            headers=headers,
        )

    try:
        status, body, headers = await service.gateway.chat(
            openai_payload, key, endpoint="chat", session_id=session_id
        )
    except (PoolError, PoolHTTPError) as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=anthropic_error(exc.status_code, str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502,
            content=anthropic_error(502, str(exc)),
        )
    if status >= 400 or (isinstance(body, dict) and "error" in body):
        message = body.get("error", {}).get("message") if isinstance(body, dict) and isinstance(body.get("error"), dict) else str(body)
        return JSONResponse(
            status_code=status,
            content=anthropic_error(status, str(message)),
        )
    return JSONResponse(
        status_code=200,
        content=to_anthropic_response(body, request_model),
        headers=headers,
    )


@router.get("/usage")
async def usage(request: Request) -> dict:
    service = _service(request)
    key = require_pool_key(request, service.store)
    day = utcnow_iso()[:10]
    return {
        "key": {
            "id": key.get("id"),
            "name": key.get("name"),
            "prefix": key.get("key_prefix"),
            "daily_request_limit": key.get("daily_request_limit"),
            "daily_credit_limit": key.get("daily_credit_limit"),
        },
        "today": service.store.key_usage_today(int(key["id"]), day),
        "total_requests": key.get("total_requests", 0),
        "total_tokens": key.get("total_tokens", 0),
        "total_credits": key.get("total_credits", 0),
    }
