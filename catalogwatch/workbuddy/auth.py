"""Admin session / pool-key authentication for the WorkBuddy console."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

from ..util import utcnow_iso
from .store import WorkBuddyStore

ADMIN_COOKIE = "cw_admin"
CSRF_COOKIE = "cw_csrf"
SESSION_HOURS = 12
POOL_KEY_PREFIX = "sk-cw-"


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _read_secret_file(name: str) -> str:
    try:
        return (_data_dir() / "secrets" / name).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


_EPHEMERAL_SECRETS: dict[str, str] = {}


def _load_or_create_secret(name: str, env_name: str, *, bytes_len: int = 32) -> str:
    value = os.environ.get(env_name, "").strip() or _read_secret_file(name)
    if value:
        return value
    if name in _EPHEMERAL_SECRETS:
        return _EPHEMERAL_SECRETS[name]
    path = _data_dir() / "secrets" / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(bytes_len)
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        return value
    except OSError:
        # Local dev without a writable /data: use a process-stable ephemeral
        # value. Pool keys created in this mode will not survive a restart.
        value = secrets.token_urlsafe(bytes_len)
        _EPHEMERAL_SECRETS[name] = value
        return value


def admin_token() -> str:
    return _load_or_create_secret("catalogwatch_admin_token", "CATALOGWATCH_ADMIN_TOKEN", bytes_len=48)


def pool_key_pepper() -> str:
    return _load_or_create_secret("pool_key_pepper", "CATALOGWATCH_KEY_PEPPER", bytes_len=48)


def hash_pool_key(key: str) -> str:
    return hmac.new(
        pool_key_pepper().encode("utf-8"), key.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def new_pool_key() -> tuple[str, str, str]:
    key = POOL_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_pool_key(key), key[:12]


def _hash_session(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _expires_at(hours: int = SESSION_HOURS) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _expired(value: str | None) -> bool:
    if not value:
        return True
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc)


def create_admin_session(
    store: WorkBuddyStore, *, user_agent: str | None = None
) -> tuple[str, str, str]:
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = _expires_at()
    store.create_session(_hash_session(session_id), expires, user_agent)
    return session_id, csrf, expires


def set_session_cookies(response: Response, session_id: str, csrf: str) -> None:
    max_age = SESSION_HOURS * 3600
    response.set_cookie(
        ADMIN_COOKIE,
        session_id,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        samesite="strict",
        secure=False,
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.netloc != request.url.netloc:
            return False
    site = request.headers.get("sec-fetch-site")
    if site and site not in {"same-origin", "none"}:
        return False
    return True


def _csrf_ok(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("x-csrf-token", "")
    return bool(cookie and header and secrets.compare_digest(cookie, header))


def require_admin(
    request: Request, store: WorkBuddyStore, *, mutation: bool = False
) -> str:
    token = _bearer(request)
    expected = admin_token()
    if token and expected and secrets.compare_digest(token, expected):
        return "bearer"

    session_id = request.cookies.get(ADMIN_COOKIE, "")
    if session_id:
        session = store.get_session(_hash_session(session_id))
        if session and not _expired(session.get("expires_at")):
            if mutation and not (_same_origin(request) and _csrf_ok(request)):
                raise HTTPException(status_code=403, detail="CSRF/origin check failed")
            store.touch_session(session["id_hash"])
            return "session"

    raise HTTPException(status_code=401, detail="Admin authentication required")


def pool_key_from_request(request: Request, store: WorkBuddyStore) -> dict | None:
    token = _bearer(request)
    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token.startswith(POOL_KEY_PREFIX):
        return None
    return store.get_pool_key_by_hash(hash_pool_key(token))


def require_pool_key(request: Request, store: WorkBuddyStore) -> dict:
    key = pool_key_from_request(request, store)
    if key is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid pool API key", "type": "invalid_request_error"}},
        )
    return key


# Login rate limit: five failures per minute per client IP. This is intentionally
# tiny and process-local; the console is a single-user loopback service.
_LOGIN_FAILURES: dict[str, list[float]] = {}


def login_allowed(client: str) -> bool:
    import time

    now = time.monotonic()
    values = [value for value in _LOGIN_FAILURES.get(client, []) if now - value < 60]
    _LOGIN_FAILURES[client] = values
    return len(values) < 5


def note_login_failure(client: str) -> None:
    import time

    _LOGIN_FAILURES.setdefault(client, []).append(time.monotonic())


def clear_login_failures(client: str) -> None:
    _LOGIN_FAILURES.pop(client, None)


def token_matches(token: str) -> bool:
    expected = admin_token()
    return bool(expected and token and secrets.compare_digest(token, expected))
