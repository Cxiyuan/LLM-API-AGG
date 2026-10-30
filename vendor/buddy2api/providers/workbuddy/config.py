"""WorkBuddy `/v3/config` catalog + promotion probe.

The official model selector reads `/v3/config`, which also carries
`data.modelPromotions`. `providers/workbuddy/models.py` reads
`/v2/enterprises/personal/models` for the served model list; this module is the
optional promotion/cost source used by the pool control plane.

Identity note: the default `current` identity uses the same billing headers as
the other WorkBuddy admin reads. `desktop`/`vscode` are explicit compatibility
probes (the reference bridge found promotions only under a desktop identity);
they are opt-in and must never be the default. Header values are overridable
via `CB_WORKBUDDY_DESKTOP_*`.
"""

from __future__ import annotations

import os

import httpx

import auth_manager

CONFIG_PATH = "/v3/config"

_DEFAULT_IDE_NAME = "VSCode"
_DEFAULT_IDE_VERSION = "1.119.0"
_DEFAULT_APP_VERSION = "4.9.29177644"


class WorkBuddyConfigError(ValueError):
    """The /v3/config request failed or returned an unusable payload."""


def parse_config(payload) -> dict:
    root = payload if isinstance(payload, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else root
    models = data.get("models") if isinstance(data.get("models"), list) else []
    promotions = (
        data.get("modelPromotions")
        if isinstance(data.get("modelPromotions"), list)
        else []
    )
    return {
        "models": models,
        "modelPromotions": promotions,
        "promotions_available": bool(promotions),
    }


def _desktop_headers(base: dict) -> dict:
    ide_name = os.environ.get("CB_WORKBUDDY_DESKTOP_IDE_NAME", _DEFAULT_IDE_NAME).strip() or _DEFAULT_IDE_NAME
    ide_version = os.environ.get("CB_WORKBUDDY_DESKTOP_IDE_VERSION", _DEFAULT_IDE_VERSION).strip() or _DEFAULT_IDE_VERSION
    app_version = os.environ.get("CB_WORKBUDDY_DESKTOP_APP_VERSION", _DEFAULT_APP_VERSION).strip() or _DEFAULT_APP_VERSION
    headers = dict(base)
    headers.update(
        {
            "X-IDE-Type": ide_name,
            "X-IDE-Name": ide_name,
            "X-IDE-Version": ide_version,
            "X-Product-Version": app_version,
            "X-Product": "SaaS",
            "User-Agent": f"{ide_name}/{ide_version} CodeBuddy/{app_version}",
        }
    )
    # CLI chat headers carry these; a desktop probe must not look like the
    # CLI stateless gateway.
    headers.pop("x-codebuddy-request", None)
    return headers


async def fetch_config(account: dict, identity: str = "current") -> dict:
    """Fetch /v3/config with the current or an explicit compatibility identity.

    Returns raw model/promotion arrays (never credentials) plus the identity
    actually used. Raises WorkBuddyConfigError on transport/HTTP/shape errors.
    """
    selected = str(identity or "current").strip().lower()
    if selected not in {"current", "desktop", "vscode"}:
        raise WorkBuddyConfigError("identity must be current, desktop, or vscode")

    if selected == "current":
        headers = await auth_manager.get_billing_headers(account)
        identity_used = "current"
    else:
        headers = await auth_manager.get_valid_headers(account)
        if headers:
            headers = _desktop_headers(headers)
        identity_used = selected
    if not headers:
        raise WorkBuddyConfigError("no usable WorkBuddy token")

    headers = dict(headers)
    headers["Accept"] = "application/json"
    headers.pop("Content-Type", None)
    url = f"{auth_manager.backend_url(account)}{CONFIG_PATH}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise WorkBuddyConfigError(f"config request failed: {exc}") from exc

    if response.status_code >= 400:
        raise WorkBuddyConfigError(f"config HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WorkBuddyConfigError("config response is not JSON") from exc

    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0):
            raise WorkBuddyConfigError(str(payload.get("msg") or payload.get("message") or code))

    parsed = parse_config(payload)
    parsed["identity_used"] = identity_used
    return parsed
