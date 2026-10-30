#!/opt/venv/bin/python
"""Container bootstrap for the WorkBuddy bridge + CatalogWatch.

Buddy2api and CatalogWatch start as separate supervisor programs. This wrapper
waits for the bridge, provisions an internal gateway key (the /v1 endpoints
require one), rewrites the provider config so the WorkBuddy provider is enabled,
then execs CatalogWatch.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

CONNECT_TIMEOUT = 3.0


def log(message: str) -> None:
    print(f"[catalogwatch-bootstrap] {message}", file=sys.stderr, flush=True)


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
    raise RuntimeError(f"Buddy2api did not become healthy in {timeout_seconds}s: {last_error}")


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
    body = {"name": name, "default_channel": "workbuddy", "daily_limit": 0}
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


def write_container_config(
    base_config: Path, out_config: Path, base_url: str
) -> Path:
    data: dict[str, Any] = yaml.safe_load(base_config.read_text(encoding="utf-8")) or {}
    providers = data.get("providers")
    if not isinstance(providers, list):
        raise RuntimeError(f"{base_config} has no providers list")

    found = False
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("id") != "workbuddy":
            continue
        provider["enabled"] = True
        provider["base_url"] = f"{base_url.rstrip('/')}/v1"
        provider["key_env"] = "WORKBUDDY_GATEWAY_KEY"
        provider["key_required"] = True
        # The WorkBuddy service owns the near-real-time cost refresh; keep the
        # generic provider poll aligned so /models does not lag behind.
        provider["poll_interval_minutes"] = 2
        found = True
    if not found:
        raise RuntimeError("the base config has no workbuddy provider entry")

    out_config.parent.mkdir(parents=True, exist_ok=True)
    out_config.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out_config


def main() -> None:
    # Keep the testable helpers above side-effect free until this point.
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    base_url = os.environ.get("BUDDY2API_URL", "http://127.0.0.1:8787").rstrip("/")
    admin_token = os.environ.get("CB_GATEWAY_ADMIN_TOKEN", "").strip()
    if not admin_token:
        admin_token = _read(data_dir / "secrets" / "admin_token")

    key_file = data_dir / "secrets" / "gateway_key"
    config_base = Path(
        os.environ.get("CATALOGWATCH_CONFIG_BASE", "/app/config/providers.yaml")
    )
    config_out = data_dir / "catalogwatch" / "providers.yaml"

    wait_for_health(base_url)
    key = ensure_gateway_key(base_url, admin_token, key_file)
    write_container_config(config_base, config_out, base_url)

    env = os.environ.copy()
    env["WORKBUDDY_GATEWAY_KEY"] = key
    env["CATALOGWATCH_CONFIG"] = str(config_out)
    env.setdefault("CATALOGWATCH_DB", str(data_dir / "catalogwatch" / "catalog.db"))
    port = env.get("CATALOGWATCH_PORT", "8788")

    if env.get("CATALOGWATCH_BOOTSTRAP_DRY_RUN") == "1":
        print(
            json.dumps(
                {
                    "ok": True,
                    "config": str(config_out),
                    "gateway_key_prefix": key[:8] + "...",
                }
            )
        )
        return

    log(f"starting CatalogWatch on 0.0.0.0:{port}")
    os.execvpe(
        "/opt/venv/bin/catalogwatch",
        ["catalogwatch", "serve", "--host", "0.0.0.0", "--port", port],
        env,
    )


if __name__ == "__main__":
    main()
