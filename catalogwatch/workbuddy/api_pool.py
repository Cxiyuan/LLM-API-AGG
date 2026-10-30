"""OpenAI-compatible pool data plane (`/v1/*`)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..util import utcnow_iso
from .auth import require_pool_key
from .gateway import PoolHTTPError, error_body
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
    for item in service.catalog.list_costs():
        model_id = item["model_id"]
        pool_id = f"{item['provider']}/{model_id}"
        if allowed_set and model_id not in allowed_set and pool_id not in allowed_set:
            continue
        data.append(
            {
                "id": pool_id,
                "object": "model",
                "created": 0,
                "owned_by": "workbuddy-pool",
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
            ) = await service.gateway.open_stream(payload, key, endpoint="chat")
        except (PoolError, PoolHTTPError) as exc:
            return _error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return StreamingResponse(
            service.gateway.stream_body(
                response, attempt, request_id, attempt_no, key_info, started
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    try:
        status, body, headers = await service.gateway.chat(payload, key, endpoint="chat")
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
            ) = await service.gateway.open_stream(payload, key, endpoint="responses")
        except (PoolError, PoolHTTPError) as exc:
            return _error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return StreamingResponse(
            service.gateway.stream_body(
                response, attempt, request_id, attempt_no, key_info, started
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    try:
        status, body, headers = await service.gateway.chat(payload, key, endpoint="responses")
    except (PoolError, PoolHTTPError) as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)
    return JSONResponse(status_code=status, content=body, headers=headers)


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
