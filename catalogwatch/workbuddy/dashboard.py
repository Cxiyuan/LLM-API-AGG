"""Serve the single-page unified model gateway console."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
_STATIC = Path(__file__).parent / "static"


@router.get("/", response_class=HTMLResponse)
@router.get("/ui", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        (_STATIC / "index.html").read_text(encoding="utf-8"),
        # The SPA shell changes with asset versions; never let a browser keep
        # an old shell that still points at removed API endpoints.
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
