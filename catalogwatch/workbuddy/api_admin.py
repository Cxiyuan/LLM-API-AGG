"""Console/admin JSON API for the WorkBuddy pool."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..util import parse_since, utcnow_iso
from .auth import (
    clear_login_failures,
    clear_session_cookies,
    create_admin_session,
    hash_pool_key,
    login_allowed,
    new_pool_key,
    note_login_failure,
    require_admin,
    set_session_cookies,
    token_matches,
)
from .direct import BRIDGE_PROVIDERS
from .model_filter import is_text_chat_model
from .router import DEFAULT_POLICY
from .service import WorkBuddyService

router = APIRouter(prefix="/api")


def _service(request: Request) -> WorkBuddyService:
    service = getattr(request.app.state, "workbuddy", None)
    if service is None:
        raise HTTPException(status_code=503, detail="WorkBuddy pool is not initialized")
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
    token = str(body.get("token") or "")
    if not token_matches(token):
        note_login_failure(client)
        raise HTTPException(status_code=401, detail="Invalid admin token")
    clear_login_failures(client)
    session_id, csrf, expires = create_admin_session(
        service.store, user_agent=request.headers.get("user-agent")
    )
    response = JSONResponse({"ok": True, "expires_at": expires})
    set_session_cookies(response, session_id, csrf)
    service.store.audit("session", "login", None, {"client": client})
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


# ------------------------------------------------------------------ overview

@router.get("/overview")
async def overview(request: Request) -> dict:
    service, _actor = _admin(request)
    bridge: dict = {"ok": False}
    try:
        bridge = await service.bridge.health()
    except Exception as exc:  # noqa: BLE001 - health is best effort
        bridge = {"ok": False, "error": str(exc)[:300]}
    return {
        "ok": True,
        "overview": service.overview(),
        "bridge": bridge,
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


# -------------------------------------------------------------------- models

def _provider_name(service: WorkBuddyService, provider_id: str) -> str:
    for cfg in service.settings.providers:
        if cfg.id == provider_id:
            return cfg.name
    return provider_id


# ------------------------------------------------------------- unified resources

@router.get("/models")
async def unified_models(request: Request) -> dict:
    """Every model resource the platform knows about, across all sources.

    `callable` answers whether the unified `/v1` gateway can serve the model
    right now: WorkBuddy needs a logged-in account, direct providers need an
    API key and a free catalog entry. Catalog-only rows stay visible with a
    reason instead of disappearing, so operators can see what is available
    versus what is wired.
    """
    service, _actor = _admin(request)
    accounts = bool(service.store.list_accounts(present=True))
    direct = getattr(service, "direct", None)
    items: list[dict[str, Any]] = []

    for cost in service.catalog.list_costs():
        provider = str(cost.get("provider") or "")
        items.append(
            {
                "source": "workbuddy",
                "kind": "workbuddy",
                "provider": provider,
                "provider_name": "WorkBuddy" if provider == "workbuddy" else "WorkBuddy 国际版",
                "model_id": cost.get("model_id"),
                "wire_id": f"{provider}/{cost.get('model_id')}",
                "name": cost.get("name") or cost.get("model_id"),
                "free": bool(cost.get("free")),
                "callable": accounts,
                "reason": None if accounts else "no_account",
                "context_length": cost.get("context_length"),
                "max_output": cost.get("max_output"),
                "credits": cost.get("credits"),
                "base_credits": cost.get("base_credits"),
                "cost_source": cost.get("cost_source"),
                "pricing": None,
            }
        )

    for row in service.store.list_catalog_models(present=True):
        provider = str(row.get("provider_id") or "")
        if provider in BRIDGE_PROVIDERS:
            continue
        model_id = str(row.get("model_id") or "")
        name = row.get("name")
        if not is_text_chat_model(model_id, name):
            continue
        is_direct = bool(direct is not None and direct.known(provider))
        ready = bool(is_direct and direct is not None and direct.provider_ready(provider))
        free = bool(row.get("is_free"))
        if not is_direct:
            reason = "catalog_only"
        elif not free:
            reason = "paid"
        elif not ready:
            reason = "no_key"
        else:
            reason = None
        items.append(
            {
                "source": provider,
                "kind": "direct",
                "provider": provider,
                "provider_name": _provider_name(service, provider),
                "model_id": model_id,
                "wire_id": f"{provider}/{model_id}",
                "name": name or model_id,
                "free": free,
                "callable": reason is None,
                "reason": reason,
                "context_length": row.get("context_length"),
                "max_output": row.get("max_output"),
                "credits": 0.0 if free else None,
                "base_credits": None,
                "cost_source": "free" if free else "paid",
                "pricing": row.get("pricing"),
            }
        )

    items.sort(
        key=lambda item: (
            0 if item.get("callable") else 1,
            0 if item.get("free") else 1,
            str(item.get("source")),
            str(item.get("model_id")),
        )
    )
    sources: dict[str, dict[str, Any]] = {}
    for item in items:
        bucket = sources.setdefault(
            str(item["source"]),
            {"source": str(item["source"]), "name": item.get("provider_name"), "total": 0, "free": 0, "callable": 0},
        )
        bucket["total"] += 1
        bucket["free"] += 1 if item.get("free") else 0
        bucket["callable"] += 1 if item.get("callable") else 0
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "free": sum(1 for item in items if item.get("free")),
            "callable": sum(1 for item in items if item.get("callable")),
            "sources": len(sources),
        },
        "sources": sorted(sources.values(), key=lambda item: item["source"]),
        "updated_at": utcnow_iso(),
    }


@router.get("/providers")
async def providers(request: Request) -> dict:
    """Provider list with key status (never the key itself)."""
    service, _actor = _admin(request)
    catalog = getattr(request.app.state, "service", None)
    status_rows = {
        row["id"]: row for row in catalog.db.providers()
    } if catalog is not None else {}
    items: list[dict[str, Any]] = []
    for cfg in service.settings.providers:
        info = (
            service.key_store.describe(cfg)
            if getattr(service, "key_store", None) is not None
            else {"configured": False, "source": None}
        )
        status = status_rows.get(cfg.id, {})
        items.append(
            {
                "id": cfg.id,
                "name": cfg.name,
                "adapter": cfg.adapter,
                "base_url": cfg.base_url,
                "key_env": cfg.key_env,
                "enabled": cfg.enabled,
                "chat_enabled": bool(cfg.chat_enabled and cfg.id not in BRIDGE_PROVIDERS),
                "configured": info.get("configured", False),
                "source": info.get("source"),
                "model_count": status.get("model_count") or 0,
                "free_model_count": status.get("free_model_count") or 0,
                "last_status": status.get("last_status"),
                "last_error": status.get("last_error"),
                "last_polled_at": status.get("last_polled_at"),
            }
        )
    return {"items": items, "updated_at": utcnow_iso()}


def _provider_config(service: WorkBuddyService, provider_id: str):
    for cfg in service.settings.providers:
        if cfg.id == provider_id:
            return cfg
    return None


@router.put("/providers/{provider_id}/key")
async def set_provider_key(provider_id: str, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    api_key = str(body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key must not be empty")
    cfg = _provider_config(service, provider_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"unknown provider: {provider_id}")
    if provider_id in BRIDGE_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="WorkBuddy uses the bridge login, not a provider API key",
        )
    store = getattr(service, "key_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="provider key storage is unavailable")
    try:
        store.set(provider_id, api_key)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _audit(service, actor, "provider.key.set", provider_id)
    return {"ok": True, **_describe_key(service, cfg)}


@router.delete("/providers/{provider_id}/key")
async def clear_provider_key(provider_id: str, request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    cfg = _provider_config(service, provider_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"unknown provider: {provider_id}")
    store = getattr(service, "key_store", None)
    if store is not None:
        store.unset(provider_id)
    _audit(service, actor, "provider.key.clear", provider_id)
    return {"ok": True, **_describe_key(service, cfg)}


@router.post("/providers/{provider_id}/poll")
async def poll_provider(provider_id: str, request: Request) -> dict:
    """Fetch this provider's catalog with the current key (tests the key)."""
    service, actor = _admin(request, mutation=True)
    catalog = getattr(request.app.state, "service", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail="catalog service is unavailable")
    if _provider_config(service, provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider: {provider_id}")
    try:
        result, _events = await catalog.poll_provider(provider_id, notify=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(service, actor, "provider.poll", provider_id, {"status": result.status})
    return {
        "ok": result.status == "ok",
        "status": result.status,
        "model_count": result.model_count,
        "free_model_count": result.free_model_count,
        "new_models": result.new_models,
        "removed_models": result.removed_models,
        "error": result.error,
    }


def _describe_key(service: WorkBuddyService, cfg) -> dict:
    store = getattr(service, "key_store", None)
    if store is None:
        return {"configured": False, "source": None}
    return store.describe(cfg)


@router.get("/catalog/free")
async def catalog_free(request: Request, q: str = "") -> dict:
    """Text-only free models from the provider directory (no bridges)."""
    service, _actor = _admin(request)
    direct = getattr(service, "direct", None)
    needle = q.strip().lower()
    items: list[dict[str, Any]] = []
    for row in service.store.list_catalog_models(present=True, free=True):
        provider = str(row.get("provider_id") or "")
        if provider in BRIDGE_PROVIDERS:
            continue
        model_id = str(row.get("model_id") or "")
        name = row.get("name")
        if not is_text_chat_model(model_id, name):
            continue
        haystack = f"{provider} {model_id} {name or ''}".lower()
        if needle and needle not in haystack:
            continue
        callable_here = bool(
            direct is not None and direct.known(provider) and direct.provider_ready(provider)
        )
        items.append(
            {
                "provider": provider,
                "provider_name": _provider_name(service, provider),
                "model_id": model_id,
                "name": name or model_id,
                "wire_id": f"{provider}/{model_id}",
                "context_length": row.get("context_length"),
                "max_output": row.get("max_output"),
                "free_kind": row.get("free_kind"),
                "pricing": row.get("pricing"),
                "callable": callable_here,
            }
        )
    items.sort(key=lambda item: (item["provider"], item["model_id"]))
    return {"items": items, "count": len(items), "updated_at": utcnow_iso()}


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
    if provider not in {"workbuddy", "workbuddy-i"} or not model_id:
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
    if provider not in {"workbuddy", "workbuddy-i"} or not model_id:
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
async def checkin_status(request: Request) -> dict:
    service, _actor = _admin(request)
    return await service.accounts.checkin_status(force=False)


@router.post("/workbuddy/checkin/run")
async def checkin_run(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    result = await service.accounts.checkin_all()
    _audit(service, actor, "checkin.run", None, {"total": result.get("total"), "claimed": result.get("claimed")})
    return result


@router.get("/workbuddy/checkin/settings")
async def checkin_settings(request: Request) -> dict:
    service, _actor = _admin(request)
    settings = await _bridge_settings(service)
    return {
        "enabled": bool(settings.get("auto_checkin_enabled", False)),
        "hour": int(settings.get("auto_checkin_hour", 9) or 9),
    }


@router.put("/workbuddy/checkin/settings")
async def update_checkin_settings(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    enabled = bool(body.get("enabled"))
    hour = int(body.get("hour") or 9)
    if not 0 <= hour <= 23:
        raise HTTPException(status_code=400, detail="hour must be 0..23")
    if enabled and not body.get("confirm_risk"):
        raise HTTPException(
            status_code=400,
            detail="Enabling automatic check-in requires confirm_risk=true",
        )
    result = await service.accounts.update_settings(
        {"auto_checkin_enabled": enabled, "auto_checkin_hour": hour}
    )
    _audit(service, actor, "checkin.settings", None, {"enabled": enabled, "hour": hour})
    return {"ok": True, "enabled": enabled, "hour": hour, "result": result}


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

@router.get("/policy")
async def get_policy(request: Request) -> dict:
    service, _actor = _admin(request)
    merged = dict(DEFAULT_POLICY)
    value = service.store.get_setting("policy", {})
    if isinstance(value, dict):
        merged.update(value)
    return merged


@router.put("/policy")
async def update_policy(request: Request) -> dict:
    service, actor = _admin(request, mutation=True)
    body = await _body(request)
    merged = dict(DEFAULT_POLICY)
    current = service.store.get_setting("policy", {})
    if isinstance(current, dict):
        merged.update(current)
    for key, value in body.items():
        if key in DEFAULT_POLICY:
            merged[key] = value
    if str(merged.get("routing_mode") or "balanced") not in {"balanced", "stable", "cost"}:
        raise HTTPException(
            status_code=400,
            detail="routing_mode must be balanced, stable, or cost",
        )
    service.store.set_setting("policy", merged)
    _audit(service, actor, "policy.update", None, merged)
    return {"ok": True, "policy": merged}


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


@router.get("/events")
async def events(request: Request, limit: int = 100) -> dict:
    service, _actor = _admin(request)
    catalog = getattr(request.app.state, "service", None)
    if catalog is None:
        return {"items": []}
    rows = catalog.db.changes(parse_since("7d"), limit=limit)
    return {"items": rows}


@router.get("/bridge/status")
async def bridge_status(request: Request) -> dict:
    service, _actor = _admin(request)
    try:
        health = await service.bridge.health()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": True, "health": health}
