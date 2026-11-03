#!/opt/venv/bin/python
"""Container bootstrap for the WorkBuddy bridge + pool control plane.

Buddy2api and the pool console start as separate supervisor programs. This
wrapper waits for the bridge, provisions an internal gateway key (the /v1
endpoints require one), then execs the unified model gateway app.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

CONNECT_TIMEOUT = 3.0


def log(message: str) -> None:
    print(f"[workbuddy-pool-bootstrap] {message}", file=sys.stderr, flush=True)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def wait_for_health(
    base_url: str,
    timeout_seconds: int = 180,
    transport: httpx.BaseTransport | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with httpx.Client(
                timeout=CONNECT_TIMEOUT, trust_env=False, transport=transport
            ) as client:
                response = client.get(f"{base_url}/health")
            if response.status_code == 200:
                log("Buddy2api is healthy")
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(
        f"Buddy2api did not become healthy in {timeout_seconds}s: {last_error}"
    )


def key_is_valid(
    base_url: str, key: str, transport: httpx.BaseTransport | None = None
) -> bool:
    if not key:
        return False
    try:
        with httpx.Client(
            timeout=CONNECT_TIMEOUT, trust_env=False, transport=transport
        ) as client:
            response = client.get(
                f"{base_url}/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def ensure_gateway_key(
    base_url: str,
    admin_token: str,
    key_file: Path,
    *,
    name: str = "catalogwatch",
    transport: httpx.BaseTransport | None = None,
) -> str:
    existing = _read(key_file)
    if key_is_valid(base_url, existing, transport=transport):
        log("reusing the existing internal gateway key")
        return existing

    if not admin_token:
        raise RuntimeError(
            "no admin token available; set CB_GATEWAY_ADMIN_TOKEN or mount /data/secrets/admin_token"
        )

    log("provisioning an internal gateway key")
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
    }
    body = {"name": name, "default_channel": "*", "daily_limit": 0}
    try:
        with httpx.Client(timeout=10.0, trust_env=False, transport=transport) as client:
            response = client.post(
                f"{base_url}/admin/api-keys", headers=headers, json=body
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not create a gateway key: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:200].replace("\n", " ")
        raise RuntimeError(
            f"could not create a gateway key: HTTP {response.status_code}: {detail}"
        )

    key = str(response.json().get("key") or "").strip()
    if not key.startswith("sk-cb-"):
        raise RuntimeError("the bridge returned no usable gateway key")

    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    if not key_is_valid(base_url, key, transport=transport):
        raise RuntimeError("the newly created gateway key was rejected by /v1/models")
    return key


def ensure_any_channel_key(
    base_url: str,
    admin_token: str,
    key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Existing internal keys were created for the CN channel only; the pool
    now serves both workbuddy and workbuddy-i, so the key must be any-channel."""
    if not admin_token or not key:
        return
    headers = {"Authorization": f"Bearer {admin_token}"}
    with httpx.Client(timeout=10.0, trust_env=False, transport=transport) as client:
        response = client.get(f"{base_url}/admin/api-keys", headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"could not list gateway keys: HTTP {response.status_code}: {response.text[:200]}"
            )
        rows = response.json()
        target = None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if row.get("key") == key or row.get("name") == "catalogwatch":
                target = row
                break
        if target is None:
            return
        if str(target.get("default_channel") or "") == "*":
            return
        updated = client.put(
            f"{base_url}/admin/api-keys/{target.get('id')}",
            headers=headers,
            json={"default_channel": "*"},
        )
        if updated.status_code >= 400:
            raise RuntimeError(
                f"could not switch the gateway key to any-channel: HTTP {updated.status_code}: {updated.text[:200]}"
            )
        log("switched the internal gateway key to any-channel")


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    base_url = os.environ.get("BUDDY2API_URL", "http://127.0.0.1:8787").rstrip("/")
    admin_token = os.environ.get("CB_GATEWAY_ADMIN_TOKEN", "").strip()
    if not admin_token:
        admin_token = _read(data_dir / "secrets" / "admin_token")

    key_file = data_dir / "secrets" / "gateway_key"

    wait_for_health(base_url)
    key = ensure_gateway_key(base_url, admin_token, key_file)
    ensure_any_channel_key(base_url, admin_token, key)

    env = os.environ.copy()
    env["WORKBUDDY_GATEWAY_KEY"] = key
    db_path = env.setdefault(
        "CATALOGWATCH_DB", str(data_dir / "catalogwatch" / "catalog.db")
    )
    port = env.get("CATALOGWATCH_PORT", "8788")

    if env.get("CATALOGWATCH_BOOTSTRAP_DRY_RUN") == "1":
        print(
            json.dumps(
                {
                    "ok": True,
                    "db": db_path,
                    "gateway_key_prefix": key[:8] + "...",
                }
            )
        )
        return

    log(f"starting the unified model gateway on 0.0.0.0:{port}")
    os.execvpe(
        "/opt/venv/bin/catalogwatch",
        ["catalogwatch", "serve", "--host", "0.0.0.0", "--port", port],
        env,
    )


if __name__ == "__main__":
    main()
