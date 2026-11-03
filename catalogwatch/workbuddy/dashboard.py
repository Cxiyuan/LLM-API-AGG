"""Legacy console entry point.

The Hono console owns the web UI on port 80.  The API process keeps a small
root route only to send anyone who opens the API port to the new console; /v1
and /api stay on their original port.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/")
@router.get("/ui")
def index(request: Request) -> RedirectResponse:
    host = request.url.hostname or "127.0.0.1"
    scheme = request.url.scheme if request.url.scheme in {"http", "https"} else "http"
    return RedirectResponse(f"{scheme}://{host}/", status_code=307)
