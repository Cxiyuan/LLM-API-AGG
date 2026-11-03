"""Console/admin JSON API for the unified model gateway."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..util import utcnow_iso
from .bridge import BridgeError
from .openbridge import OpenBridgeError
from .auth import (
    clear_login_failures,
    clear_session_cookies,
    create_admin_session,
    credentials_match,
    hash_pool_key,
    login_allowed,
    new_pool_key,
    note_login_failure,
    require_admin,
    set_session_cookies,
)
from .promotions import parse_clock_minutes
from .providers import BRIDGE_PROVIDERS, WORKBUDDY_PROVIDERS
from .router import DEFAULT_POLICY
from .schedule import (
    DEFAULT_DAY_PRIORITY,
    DEFAULT_NIGHT_PRIORITY,
    is_full_price,
    night_window,
    period_now,
    tier_bucket,
)
from .service import WorkBuddyService

router = APIRouter(prefix="/api")


def _service(request: Request) -> WorkBuddyService:
    service = getattr(request.app.state, "workbuddy", None)
    if service is None:
        raise HTTPException(status_code=503, detail="model gateway is not initialized")
    return service


def _admin(request: Request, *, mutation: bool = False) -> tuple[WorkBuddyService, str]:
    service = _service(request)
    actor = require_admin(request, service.store, mutation=mutation)
    return service, actor


async def _body(request: Request) -> dict:
    try:
        value = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="JSON body required") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return value


def _audit(service: WorkBuddyService, actor: str, action: str, target: str | None = None, payload: Any = None) -> None:
    service.store.audit(actor, action, target, payload)


# ---------------------------------------------------------------------- auth

@router.post("/auth/login")
async def login(request: Request) -> JSONResponse:
    service = _service(request)
    client = request.client.host if request.client else "unknown"
    if not login_allowed(client):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    body = await _body(request)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not credentials_match(username, password):
        note_login_failure(client)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    clear_login_failures(client)
    session_id, csrf, expires = create_admin_session(
        service.store, user_agent=request.headers.get("user-agent")
    )
    response = JSONResponse({"ok": True, "expires_at": expires})
    set_session_cookies(response, session_id, csrf)
    service.store.audit(username or "session", "login", None, {"client": client})
    return response


@router.post("/auth/logout")
async def logout(request: Request) -> JSONResponse:
    service = _service(request)
    session_id = request.cookies.get("cw_admin", "")
    if session_id:
        from .auth import _hash_session

        service.store.delete_admin_session(_hash_session(session_id))
    response = JSONResponse({"ok": True})
    clear_session_cookies(response)
    return response


@router.get("/auth/session")
async def session(request: Request) -> dict:
    service, actor = _admin(request)
    return {"ok": True, "actor": actor}


# --------------------------------------------------------------- openbridge

@router.get("/openbridge/config")
async def openbridge_config(request: Request) -> dict:
    service, _actor = _admin(request)
    return service.openbridge_config()


@router.put("/openbridge/config")
async def update_openbridge_config(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    try:
        result = await service.configure_openbridge(
            url=str(body.get("url") or ""),
            api_key=(str(body["api_key"]) if "api_key" in body else None),
            clear_api_key=bool(body.get("clear_api_key")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await service.refresh_catalog(force=True)
    except Exception:  # noqa: BLE001 - config is saved; next scheduler retries
        pass
    _audit(service, actor, "openbridge.config", None, {"url": result.get("url"), "configured": result.get("configured")})
    return {"ok": True, "config": result}


@router.post("/openbridge/proxy")
async def openbridge_proxy(request: Request) -> dict:
    """Console-only proxy so the OpenBridge auth key never reaches the browser."""
    service, _actor = _admin(request, mutation=True)
    body = await _body(request)
    path = str(body.get("path") or "").strip()
    method = str(body.get("method") or "GET").upper()
    if not (path == "/health" or path.startswith("/v1/")):
        raise HTTPException(status_code=400, detail="invalid OpenBridge path")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPException(status_code=400, detail="invalid OpenBridge method")
    payload = body.get("body")
    if payload is not None and not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="OpenBridge body must be an object")
    if not service.openbridge.configured:
        raise HTTPException(status_code=503, detail="OpenBridge 尚未配置")
    try:
        data = await service.openbridge.request(method, path, payload)
    except OpenBridgeError as exc:
        raise HTTPException(
            status_code=exc.status_code if 400 <= exc.status_code < 600 else 502,
            detail=str(exc),
        ) from exc
    return {"ok": True, "data": data}


# ------------------------------------------------------------------ overview

@router.get("/overview")
async def overview(request: Request) -> dict:
    service, _actor = _admin(request)
    bridge: dict = {"ok": False}
    try:
        bridge = await asyncio.wait_for(service.bridge.health(), timeout=4.0)
        # Buddy2api's /health reports {"status": "ok", ...}; normalize the flag
        # so clients that look for `ok` do not show a false "unreachable".
        if isinstance(bridge, dict):
            bridge.setdefault(
                "ok", str(bridge.get("status") or "").strip().lower() == "ok"
            )
    except Exception as exc:  # noqa: BLE001 - health is best effort
        bridge = {"ok": False, "error": str(exc)[:300]}
    openbridge: dict = {"ok": False, "configured": service.openbridge.configured}
    if service.openbridge.configured:
        try:
            openbridge = {
                **openbridge,
                **(await asyncio.wait_for(service.openbridge.health(), timeout=3.0)),
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001 - health is best effort
            openbridge = {**openbridge, "error": str(exc)[:300]}
    return {
        "ok": True,
        "overview": service.overview(),
        "bridge": bridge,
        "openbridge": openbridge,
        "policy": service.router.policy() if hasattr(service, "router") else service.store.get_setting("policy", DEFAULT_POLICY),
        "settings": {
            "catalog_refresh_seconds": service.store.get_setting("catalog_refresh_seconds", 60),
            "credit_refresh_seconds": service.store.get_setting("credit_refresh_seconds", 300),
            "account_sync_seconds": service.store.get_setting("account_sync_seconds", 60),
            "config_identity": service.catalog.identity(),
        },
        "config_snapshot": service.store.latest_config_snapshot(),
        "updated_at": utcnow_iso(),
    }


@router.get("/workbuddy/models")
async def models(request: Request) -> dict:
    service, _actor = _admin(request)
    costs = service.catalog.list_costs()
    return {
        "items": costs,
        "summary": {
            "total": len(costs),
            "free": sum(1 for item in costs if item.get("free")),
            "known_cost": sum(1 for item in costs if item.get("credits") is not None),
            "unknown_cost": sum(1 for item in costs if item.get("credits") is None),
        },
        "identity": service.catalog.identity(),
        "config_snapshot": service.store.latest_config_snapshot(),
    }


@router.post("/workbuddy/catalog/refresh")
async def refresh_catalog(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    result = await service.refresh_catalog(force=True)
    _audit(service, actor, "catalog.refresh", None, result)
    return {"ok": True, "results": result}


@router.put("/workbuddy/models/manual")
async def set_manual_cost(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    provider = str(body.get("provider") or "").strip()
    model_id = str(body.get("model_id") or "").strip()
    if provider not in BRIDGE_PROVIDERS or not model_id:
        raise HTTPException(status_code=400, detail="provider and model_id are required")
    windows = body.get("daily_windows")
    if windows is not None and not isinstance(windows, list):
        raise HTTPException(status_code=400, detail="daily_windows must be an array")
    service.store.upsert_manual_cost(
        provider,
        model_id,
        base_credits=body.get("base_credits"),
        factor=body.get("factor"),
        daily_windows=windows or [],
        timezone_name=str(body.get("timezone") or "Asia/Shanghai"),
        valid_from=body.get("valid_from"),
        valid_until=body.get("valid_until"),
        enabled=bool(body.get("enabled", True)),
    )
    _audit(service, actor, "model.manual_cost", f"{provider}/{model_id}", body)
    return {"ok": True}


@router.delete("/workbuddy/models/manual")
async def delete_manual_cost(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    provider = str(request.query_params.get("provider") or "").strip()
    model_id = str(request.query_params.get("model_id") or "").strip()
    if provider not in BRIDGE_PROVIDERS or not model_id:
        raise HTTPException(status_code=400, detail="provider and model_id are required")
    service.store.delete_manual_cost(provider, model_id)
    _audit(service, actor, "model.manual_cost.delete", f"{provider}/{model_id}")
    return {"ok": True}


# ------------------------------------------------------------------- accounts

@router.get("/workbuddy/accounts")
async def accounts(request: Request) -> dict:
    service, _actor = _admin(request)
    return {"items": service.accounts.summary()}


@router.post("/workbuddy/accounts/sync")
async def sync_accounts(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    items = await service.refresh_accounts()
    _audit(service, actor, "accounts.sync", None, {"count": len(items)})
    return {"ok": True, "items": items}


@router.post("/workbuddy/login/start")
async def login_start(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    result = await service.accounts.login_start(str(body.get("realm") or "cn"))
    _audit(service, actor, "account.login.start", None, {"realm": body.get("realm")})
    return result


@router.get("/workbuddy/login/poll")
async def login_poll(request: Request, state: str = "") -> dict:
    service, _actor = _admin(request)
    if not state.strip():
        raise HTTPException(status_code=400, detail="state is required")
    result = await service.accounts.login_poll(state)
    if isinstance(result, dict) and result.get("done"):
        _audit(service, actor="session", action="account.login.done", payload=result)
    return result


@router.get("/workbuddy/channels")
async def bridge_channels(request: Request) -> dict:
    service, _actor = _admin(request)
    return await service.accounts.channels()


@router.post("/workbuddy/accounts/add")
async def add_account(request: Request) -> dict:
    """Paste-token import for Qoder / QwenWork / TraeWork.

    WorkBuddy still uses the browser OAuth flow; the other channels expose a
    provider-specific credential parser, so their tokens are pasted here and
    stored only in the bridge's encrypted database.
    """
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    provider = str(body.get("provider") or body.get("channel") or "").strip()
    if provider not in BRIDGE_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"unsupported provider: {provider}")
    if provider in WORKBUDDY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="WorkBuddy 请在页面上点“添加账号”走浏览器授权；粘贴导入只用于 Qoder/QwenWork/TraeWork",
        )
    body["provider"] = provider
    try:
        result = await service.accounts.add_account(body)
    except BridgeError as exc:
        raise HTTPException(
            status_code=exc.status_code if 400 <= exc.status_code < 500 else 502,
            detail=str(exc),
        ) from exc
    try:
        await service.refresh_catalog(force=True)
    except Exception:  # noqa: BLE001 - import already succeeded; catalog can retry later
        pass
    _audit(service, actor, "accounts.add", None, {"provider": provider})
    return result


@router.get("/workbuddy/accounts/discover")
async def discover_accounts(request: Request) -> dict:
    service, _actor = _admin(request)
    return await service.accounts.discover(dict(request.query_params))


@router.post("/workbuddy/accounts/import")
async def import_accounts(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    result = await service.accounts.import_accounts(body)
    _audit(service, actor, "accounts.import", None, {"channel": body.get("channel")})
    return result


@router.patch("/workbuddy/accounts/{account_id}")
async def update_account(account_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    result = await service.accounts.update_account(account_id, body)
    _audit(service, actor, "account.update", str(account_id), body)
    return result


@router.delete("/workbuddy/accounts/{account_id}")
async def delete_account(account_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    await service.accounts.delete_account(account_id)
    _audit(service, actor, "account.delete", str(account_id))
    return {"ok": True}


@router.post("/workbuddy/accounts/{account_id}/refresh")
async def refresh_account(account_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    result = await service.accounts.refresh_account(account_id)
    _audit(service, actor, "account.refresh", str(account_id))
    return {"ok": True, "result": result}


@router.post("/workbuddy/accounts/{account_id}/test")
async def test_account(account_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request) if request.headers.get("content-length") not in {None, "0"} else {}
    result = await service.accounts.test_account(account_id, body)
    _audit(service, actor, "account.test", str(account_id), {"ok": result.get("ok")})
    return result


@router.put("/workbuddy/accounts/{account_id}/caps")
async def set_caps(account_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    model_id = str(body.get("model_id") or "*")
    service.store.set_cap(
        account_id,
        model_id,
        daily_request_cap=int(body.get("daily_request_cap") or 0),
        daily_credit_cap=float(body.get("daily_credit_cap") or 0),
    )
    _audit(service, actor, "account.caps", str(account_id), {"model_id": model_id, **body})
    return {"ok": True}


# -------------------------------------------------------------------- credits

@router.get("/workbuddy/credits")
async def credits(request: Request) -> dict:
    service, _actor = _admin(request)
    items = []
    for account in service.store.list_accounts(present=True):
        items.append(
            {
                "account_id": account["account_id"],
                "name": account.get("nickname") or account.get("name"),
                "provider": account.get("provider"),
                "available_credits": account.get("available_credits"),
                "credit_expiring_30d": account.get("credit_expiring_30d"),
                "latest": service.store.latest_credit(int(account["account_id"])),
                "state": service.store.get_state(int(account["account_id"])),
            }
        )
    return {"items": items}


@router.post("/workbuddy/credits/refresh")
async def refresh_credits(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    result = await service.refresh_credits(force=True)
    _audit(service, actor, "credits.refresh", None, {"ok": result.get("ok")})
    return result


# --------------------------------------------------------------- check-in

async def _bridge_settings(service: WorkBuddyService) -> dict:
    settings = await service.accounts.settings()
    return settings if isinstance(settings, dict) else {}


@router.get("/workbuddy/checkin")
async def checkin_status(request: Request, force: int = 0) -> dict:
    service, _actor = _admin(request)
    return await service.checkin_overview(force=bool(force))


@router.post("/workbuddy/checkin/run")
async def checkin_run(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    result = await service.run_all_checkins()
    _audit(service, actor, "checkin.run_all", None, {"total": result.get("total"), "claimed": result.get("claimed")})
    return result


@router.get("/workbuddy/checkin/settings")
async def checkin_settings(request: Request) -> dict:
    service, _actor = _admin(request)
    return service.checkin_global()


@router.put("/workbuddy/checkin/settings")
async def update_checkin_settings(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    enabled = bool(body.get("enabled"))
    hour = int(body.get("hour") or 0)
    minute = int(body.get("minute") or 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise HTTPException(status_code=400, detail="hour must be 0..23 and minute 0..59")
    if enabled and not body.get("confirm_risk"):
        raise HTTPException(
            status_code=400,
            detail="Enabling automatic check-in requires confirm_risk=true",
        )
    service.store.set_setting("auto_checkin_enabled", enabled)
    service.store.set_setting("auto_checkin_hour", hour)
    service.store.set_setting("auto_checkin_minute", minute)
    try:
        await service.accounts.update_settings({"auto_checkin_enabled": False})
        service._bridge_auto_checkin_off = True
    except Exception:  # noqa: BLE001 - pool scheduler remains authoritative
        pass
    _audit(service, actor, "checkin.settings", None, {"enabled": enabled, "hour": hour, "minute": minute})
    return {"ok": True, "enabled": enabled, "hour": hour, "minute": minute}


@router.put("/workbuddy/checkin/accounts/{account_id}")
async def update_account_checkin(request: Request, account_id: int) -> dict:
    service, actor = _admin(request, mutation=True)
    if int(account_id) not in {
        int(row["account_id"]) for row in service.store.list_accounts(present=True)
    }:
        raise HTTPException(status_code=404, detail="account not found")
    body = await _body(request)
    enabled = bool(body.get("enabled"))
    hour = int(body.get("hour") or 0)
    minute = int(body.get("minute") or 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise HTTPException(status_code=400, detail="hour must be 0..23 and minute 0..59")
    service.store.set_checkin_rule(
        int(account_id), enabled=enabled, hour=hour, minute=minute
    )
    _audit(
        service,
        actor,
        "checkin.rule.set",
        str(account_id),
        {"enabled": enabled, "hour": hour, "minute": minute},
    )
    return {"ok": True, "account_id": int(account_id), "enabled": enabled, "hour": hour, "minute": minute}


@router.delete("/workbuddy/checkin/accounts/{account_id}")
async def clear_account_checkin(request: Request, account_id: int) -> dict:
    service, actor = _admin(request, mutation=True)
    service.store.delete_checkin_rule(int(account_id))
    _audit(service, actor, "checkin.rule.clear", str(account_id))
    return {"ok": True, "account_id": int(account_id)}


@router.get("/workbuddy/accounts/{account_id}/checkin")
async def account_checkin_status(request: Request, account_id: int, force: int = 1) -> dict:
    service, _actor = _admin(request)
    overview = await service.checkin_overview(force=bool(force))
    for item in overview.get("accounts", []):
        if int(item.get("account_id") or 0) == int(account_id):
            return {"ok": True, "account_id": int(account_id), "item": item}
    raise HTTPException(status_code=404, detail="account not found")


@router.post("/workbuddy/accounts/{account_id}/checkin")
async def account_checkin(request: Request, account_id: int) -> dict:
    service, actor = _admin(request, mutation=True)
    if int(account_id) not in {
        int(row["account_id"]) for row in service.store.list_accounts(present=True)
    }:
        raise HTTPException(status_code=404, detail="account not found")
    result = await service.run_checkin(int(account_id))
    _audit(
        service,
        actor,
        "checkin.run",
        str(account_id),
        {"ok": result.get("ok"), "claimed": result.get("claimed"), "message": result.get("message")},
    )
    return result


@router.get("/workbuddy/keepalive/settings")
async def keepalive_settings(request: Request) -> dict:
    service, _actor = _admin(request)
    settings = await _bridge_settings(service)
    return {
        "enabled": bool(settings.get("keepalive_enabled", False)),
        "hour": int(settings.get("keepalive_hour", 22) or 22),
    }


@router.put("/workbuddy/keepalive/settings")
async def update_keepalive_settings(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    enabled = bool(body.get("enabled"))
    hour = int(body.get("hour") or 22)
    if not 0 <= hour <= 23:
        raise HTTPException(status_code=400, detail="hour must be 0..23")
    result = await service.accounts.update_settings(
        {"keepalive_enabled": enabled, "keepalive_hour": hour}
    )
    _audit(service, actor, "keepalive.settings", None, {"enabled": enabled, "hour": hour})
    return {"ok": True, "enabled": enabled, "hour": hour, "result": result}


@router.get("/workbuddy/automation-logs")
async def automation_logs(request: Request, limit: int = 100, failures_only: bool = False) -> Any:
    service, _actor = _admin(request)
    return await service.bridge.automation_logs(limit=limit, failures_only=failures_only)


# ----------------------------------------------------------------------- keys

@router.get("/keys")
async def list_keys(request: Request) -> dict:
    service, _actor = _admin(request)
    return {"items": service.store.list_pool_keys()}


@router.post("/keys")
async def create_key(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    key, key_hash, prefix = new_pool_key()
    kind = str(body.get("kind") or "agent")
    if kind not in {"agent", "shared"}:
        raise HTTPException(status_code=400, detail="kind must be agent or shared")
    allowed = body.get("allowed_models")
    if allowed is not None and not isinstance(allowed, list):
        raise HTTPException(status_code=400, detail="allowed_models must be an array")
    key_id = service.store.create_pool_key(
        name=str(body.get("name") or "pool key")[:120],
        key_hash=key_hash,
        key_prefix=prefix,
        kind=kind,
        allowed_models=[str(item) for item in allowed] if allowed else None,
        daily_request_limit=int(body.get("daily_request_limit") or 0),
        daily_credit_limit=float(body.get("daily_credit_limit") or 0),
        max_attempts=int(body.get("max_attempts") or 3),
    )
    _audit(service, actor, "key.create", str(key_id), {"name": body.get("name"), "kind": kind})
    return {"ok": True, "id": key_id, "key": key, "prefix": prefix}


@router.patch("/keys/{key_id}")
async def update_key(key_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    fields: dict[str, Any] = {}
    for key in ("name", "status", "daily_request_limit", "daily_credit_limit", "max_attempts"):
        if key in body:
            fields[key] = body[key]
    if "allowed_models" in body:
        allowed = body["allowed_models"]
        if allowed is not None and not isinstance(allowed, list):
            raise HTTPException(status_code=400, detail="allowed_models must be an array")
        fields["allowed_models_json"] = (
            json.dumps([str(item) for item in allowed]) if allowed else None
        )
    service.store.update_pool_key(key_id, fields)
    _audit(service, actor, "key.update", str(key_id), {k: v for k, v in fields.items() if k != "key_hash"})
    return {"ok": True}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    service.store.delete_pool_key(key_id)
    _audit(service, actor, "key.delete", str(key_id))
    return {"ok": True}


@router.post("/keys/{key_id}/rotate")
async def rotate_key(key_id: int, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    old = service.store.get_pool_key(key_id)
    if old is None:
        raise HTTPException(status_code=404, detail="key not found")
    key, key_hash, prefix = new_pool_key()
    new_id = service.store.create_pool_key(
        name=old.get("name") or "pool key",
        key_hash=key_hash,
        key_prefix=prefix,
        kind=old.get("kind") or "agent",
        allowed_models=old.get("allowed_models"),
        daily_request_limit=int(old.get("daily_request_limit") or 0),
        daily_credit_limit=float(old.get("daily_credit_limit") or 0),
        max_attempts=int(old.get("max_attempts") or 3),
    )
    service.store.delete_pool_key(key_id)
    _audit(service, actor, "key.rotate", str(key_id), {"new_id": new_id})
    return {"ok": True, "id": new_id, "key": key, "prefix": prefix}


# ------------------------------------------------------------------ requests

@router.get("/requests")
async def requests_list(request: Request, limit: int = 100, offset: int = 0) -> dict:
    service, _actor = _admin(request)
    filters = dict(request.query_params)
    return {
        "items": service.store.list_requests(limit=limit, offset=offset, filters=filters)
    }


@router.get("/requests/{request_id}")
async def request_detail(request: Request, request_id: str) -> dict:
    service, _actor = _admin(request)
    item = service.store.get_request(request_id)
    if item is None:
        raise HTTPException(status_code=404, detail="request not found")
    return {"item": item, "attempts": service.store.list_attempts(request_id)}


# -------------------------------------------------------------------- policy

_PRIORITY_DEFAULTS = {
    "day_priority": DEFAULT_DAY_PRIORITY,
    "night_priority": DEFAULT_NIGHT_PRIORITY,
}


def _merged_policy(service: WorkBuddyService) -> dict:
    merged = dict(DEFAULT_POLICY)
    value = service.store.get_setting("policy", {})
    if isinstance(value, dict):
        merged.update(value)
    for key, defaults in _PRIORITY_DEFAULTS.items():
        normalized = dict(defaults)
        configured = merged.get(key)
        if isinstance(configured, dict):
            for bucket, rank in configured.items():
                if str(bucket) in defaults:
                    try:
                        normalized[str(bucket)] = int(rank)
                    except (TypeError, ValueError):
                        continue
        merged[key] = normalized
    for key, fallback in (("night_start", "23:00"), ("night_end", "08:00")):
        if parse_clock_minutes(merged.get(key)) is None:
            merged[key] = fallback
    return merged


@router.get("/policy")
async def get_policy(request: Request) -> dict:
    service, _actor = _admin(request)
    return _merged_policy(service)


@router.put("/policy")
async def update_policy(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    for key, defaults in _PRIORITY_DEFAULTS.items():
        if key in body:
            value = body[key]
            if not isinstance(value, dict):
                raise HTTPException(status_code=400, detail=f"{key} must be an object")
            unknown = {str(bucket) for bucket in value} - set(defaults)
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} has unknown buckets: {', '.join(sorted(unknown))}",
                )
            for bucket, rank in value.items():
                try:
                    int(rank)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key}.{bucket} must be an integer",
                    ) from exc
    for key in ("night_start", "night_end"):
        if key in body and parse_clock_minutes(body[key]) is None:
            raise HTTPException(status_code=400, detail=f"{key} must be HH:MM")
    merged = _merged_policy(service)
    for key, value in body.items():
        if key in DEFAULT_POLICY:
            merged[key] = value
    if str(merged.get("routing_mode") or "balanced") not in {"balanced", "stable", "cost"}:
        raise HTTPException(
            status_code=400,
            detail="routing_mode must be balanced, stable, or cost",
        )
    if str(merged.get("account_sharing") or "balanced") not in {
        "balanced",
        "cost_first",
    }:
        raise HTTPException(
            status_code=400,
            detail="account_sharing must be balanced or cost_first",
        )
    merged = _merged_policy_for_update(merged)
    service.store.set_setting("policy", merged)
    _audit(service, actor, "policy.update", None, merged)
    return {"ok": True, "policy": merged}


def _merged_policy_for_update(policy: dict) -> dict:
    """Normalize nested priority maps after validating the submitted body."""
    result = dict(policy)
    for key, defaults in _PRIORITY_DEFAULTS.items():
        normalized = dict(defaults)
        configured = policy.get(key)
        if isinstance(configured, dict):
            for bucket, rank in configured.items():
                if str(bucket) in defaults:
                    normalized[str(bucket)] = int(rank)
        result[key] = normalized
    return result


@router.get("/schedule")
async def get_schedule(request: Request) -> dict:
    service, _actor = _admin(request)
    policy = _merged_policy(service)
    costs = service.catalog.list_costs()
    info = period_now(policy, costs)
    allow_night = bool(policy.get("allow_night", True))
    deny_full = bool(policy.get("deny_full_price", True))
    models = []
    for item in costs:
        bucket = tier_bucket(item, info["period"], allow_night=allow_night)
        models.append(
            {
                "provider": item.get("provider"),
                "model_id": item.get("model_id"),
                "name": item.get("name"),
                "credits": item.get("credits"),
                "free": bool(item.get("free")),
                "promotion_active": bool(item.get("promotion_active")),
                "promotion_factor": item.get("promotion_factor"),
                "promotion_label": item.get("promotion_label"),
                "night_specific": bool(item.get("night_specific")),
                "day_credits": item.get("day_credits"),
                "night_credits": item.get("night_credits"),
                "price_windows": item.get("price_windows") or [],
                "bucket": bucket,
                "full_price": is_full_price(item),
                "eligible": (not deny_full) or (not is_full_price(item)),
            }
        )
    start, end, source = night_window(policy, costs)
    return {
        "now": utcnow_iso(),
        "timezone": "Asia/Shanghai",
        "period": info["period"],
        "night_window": {
            "start": f"{start // 60:02d}:{start % 60:02d}",
            "end": f"{end // 60:02d}:{end % 60:02d}",
            "source": source,
        },
        "day_priority": policy.get("day_priority"),
        "night_priority": policy.get("night_priority"),
        "account_sharing": policy.get("account_sharing", "balanced"),
        "deny_full_price": bool(policy.get("deny_full_price", True)),
        "providers": service.router.share_snapshot().get("providers", {}),
        "models": models,
    }


# ----------------------------------------------------------------- settings

@router.get("/settings")
async def get_settings(request: Request) -> dict:
    service, _actor = _admin(request)
    return {
        "catalog_refresh_seconds": service.store.get_setting("catalog_refresh_seconds", 60),
        "credit_refresh_seconds": service.store.get_setting("credit_refresh_seconds", 300),
        "account_sync_seconds": service.store.get_setting("account_sync_seconds", 60),
        "config_identity": service.catalog.identity(),
    }


@router.put("/settings")
async def update_settings(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    allowed = {
        "catalog_refresh_seconds": (30, 600),
        "credit_refresh_seconds": (60, 3600),
        "account_sync_seconds": (15, 900),
    }
    changed = {}
    for key, (minimum, maximum) in allowed.items():
        if key in body:
            value = max(minimum, min(maximum, int(body[key])))
            service.store.set_setting(key, value)
            changed[key] = value
    if "config_identity" in body:
        identity = str(body["config_identity"]).lower()
        if identity not in {"current", "desktop", "vscode"}:
            raise HTTPException(status_code=400, detail="config_identity must be current/desktop/vscode")
        service.store.set_setting("config_identity", identity)
        changed["config_identity"] = identity
    _audit(service, actor, "settings.update", None, changed)
    return {"ok": True, "settings": changed}


@router.get("/audit")
async def audit(request: Request, limit: int = 100) -> dict:
    service, _actor = _admin(request)
    return {"items": service.store.list_audit(limit=limit)}


@router.get("/bridge/status")
async def bridge_status(request: Request) -> dict:
    service, _actor = _admin(request)
    try:
        health = await service.bridge.health()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": True, "health": health}
