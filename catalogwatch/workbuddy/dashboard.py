"""Serve the single-page WorkBuddy pool console."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
_STATIC = Path(__file__).parent / "static"


@router.get("/", response_class=HTMLResponse)
@router.get("/ui", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))
