from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .db import Database
from .workbuddy.api_admin import router as admin_router
from .workbuddy.api_pool import router as pool_router
from .workbuddy.bridge import BridgeError
from .workbuddy.dashboard import router as dashboard_router
from .workbuddy.gateway import PoolHTTPError, error_body
from .workbuddy.router import PoolError
from .workbuddy.service import WorkBuddyService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        pool = WorkBuddyService(db)
        app.state.workbuddy = pool
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        if os.getenv("CATALOGWATCH_POOL_SCHEDULER", "1").lower() not in {
            "0",
            "false",
            "no",
        }:
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
                db.close()

    app = FastAPI(
        title="WorkBuddy Pool",
        version="0.2.0",
        description="WorkBuddy multi-account pool gateway and console.",
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
    async def _pool_http_error_handler(
        request: Request, exc: PoolHTTPError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message[:500]),
        )

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        pool: WorkBuddyService = request.app.state.workbuddy
        db = pool.store.db
        workbuddy: dict[str, Any] = {}
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
            "db": str(db.path),
            "stats": db.stats(),
            "workbuddy": workbuddy,
        }

    app.include_router(admin_router)
    app.include_router(pool_router)
    app.include_router(dashboard_router)
    static_dir = Path(__file__).parent / "workbuddy" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
