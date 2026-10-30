from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .service import CatalogService
from .util import parse_since
from .workbuddy.api_admin import router as admin_router
from .workbuddy.api_pool import router as pool_router
from .workbuddy.bridge import BridgeError
from .workbuddy.dashboard import router as dashboard_router
from .workbuddy.gateway import PoolHTTPError, error_body
from .workbuddy.router import PoolError
from .workbuddy.service import WorkBuddyService


def _service(request: Request) -> CatalogService:
    return request.app.state.service


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service = CatalogService(settings)
        pool = WorkBuddyService(settings, service.db, key_store=service.key_store)
        app.state.service = service
        app.state.workbuddy = pool
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        if os.getenv("CATALOGWATCH_SCHEDULER", "1").lower() not in {"0", "false", "no"}:
            tasks.append(asyncio.create_task(service.run_forever(stop)))
        if os.getenv("CATALOGWATCH_POOL_SCHEDULER", "1").lower() not in {"0", "false", "no"}:
            tasks.append(asyncio.create_task(pool.run_forever(stop)))
        try:
            yield
        finally:
            stop.set()
            for task in tasks:
                try:
                    await asyncio.wait_for(task, timeout=20)
                except asyncio.TimeoutError:
                    task.cancel()
            try:
                await pool.aclose()
            finally:
                service.close()

    app = FastAPI(
        title="CatalogWatch",
        version="0.1.0",
        description="Near-real-time catalog of free LLM models and provider quota pages.",
        lifespan=lifespan,
    )

    @app.exception_handler(BridgeError)
    async def _bridge_error_handler(request: Request, exc: BridgeError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=error_body("bridge_error", str(exc)[:500]),
        )

    @app.exception_handler(PoolError)
    async def _pool_error_handler(request: Request, exc: PoolError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, str(exc)[:500]),
        )

    @app.exception_handler(PoolHTTPError)
    async def _pool_http_error_handler(request: Request, exc: PoolHTTPError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message[:500]),
        )

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        service = _service(request)
        pool = getattr(request.app.state, "workbuddy", None)
        workbuddy: dict[str, Any] = {}
        if pool is not None:
            try:
                workbuddy = {
                    "accounts": len(pool.store.list_accounts(present=True)),
                    "models": len(pool.store.list_models()),
                    "bridge_configured": bool(
                        pool.bridge.admin_token and pool.bridge.gateway_key
                    ),
                }
            except Exception as exc:  # noqa: BLE001 - health must not crash
                workbuddy = {"error": str(exc)[:200]}
        return {
            "ok": True,
            "db": str(service.db.path),
            "stats": service.db.stats(),
            "workbuddy": workbuddy,
        }

    @app.get("/stats")
    def stats(request: Request) -> dict[str, Any]:
        return _service(request).db.stats()

    @app.get("/providers")
    def providers(request: Request) -> dict[str, Any]:
        return {"items": _service(request).db.providers()}

    @app.get("/models")
    def models(
        request: Request,
        free: bool | None = None,
        present: bool = True,
        provider: str | None = None,
        q: str | None = None,
        limit: int = Query(200, ge=1, le=5000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        items = _service(request).db.models(
            present=present,
            free=free,
            provider=provider,
            query=q,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "limit": limit, "offset": offset, "count": len(items)}

    @app.get("/catalog")
    def catalog(request: Request, free_only: bool = True) -> dict[str, Any]:
        return _service(request).db.export_catalog(free_only=free_only)

    @app.get("/changes")
    def changes(
        request: Request,
        since: str = "24h",
        limit: int = Query(100, ge=1, le=1000),
        kind: str | None = None,
    ) -> dict[str, Any]:
        try:
            since_iso = parse_since(since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid since: {exc}") from exc
        kinds = [item.strip() for item in kind.split(",") if item.strip()] if kind else None
        items = _service(request).db.changes(since_iso, limit=limit, kinds=kinds)
        return {"since": since_iso, "items": items, "count": len(items)}

    @app.get("/quota/sources")
    def quota_sources(request: Request) -> dict[str, Any]:
        return {"items": _service(request).db.quota_overview()}

    @app.post("/poll")
    async def poll(
        request: Request,
        provider: str | None = None,
        quota: bool = False,
        notify: bool = False,
    ) -> dict[str, Any]:
        service = _service(request)
        if provider:
            try:
                result, events = await service.poll_provider(provider, notify=notify)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            results = [result]
            event_count = len(events)
        else:
            results, events = await service.poll_all(force=True, notify=notify)
            event_count = len(events)

        quota_events = await service.check_quotas(force=True, notify=notify) if quota else []
        return {
            "results": [
                {
                    "provider_id": result.provider_id,
                    "status": result.status,
                    "model_count": result.model_count,
                    "free_model_count": result.free_model_count,
                    "new_models": result.new_models,
                    "removed_models": result.removed_models,
                    "error": result.error,
                }
                for result in results
            ],
            "events": event_count,
            "quota_events": len(quota_events),
        }

    app.include_router(admin_router)
    app.include_router(pool_router)
    app.include_router(dashboard_router)
    static_dir = Path(__file__).parent / "workbuddy" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
