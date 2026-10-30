"""WorkBuddy supplier-list fetch.

Official desktop CloudAgentService.listAvailableModels:
GET /v2/enterprises/personal/models, unwrap json.data ?? json, then data.models.
Skip rows whose tags include text-to-image.
"""

from __future__ import annotations

import re

import httpx

import auth_manager
from model_capacity import capacity_fields
from model_reasoning import reasoning_fields

MODELS_PATH = "/v2/enterprises/personal/models"
_NON_CHAT_TAGS = frozenset({"text-to-image"})
_COST_FIELDS = (
    "tags",
    "supportsImages",
    "supportsToolCall",
    "supportsReasoning",
    "onlyReasoning",
    "contextWindow",
)


def _credits_value(row: dict):
    """WorkBuddy sends credits as a number or a string like "x0.79"."""
    raw = row.get("credits")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        hit = re.search(r"x\s*([0-9]*\.?[0-9]+)", raw, re.IGNORECASE)
        if hit:
            return float(hit.group(1))
    return None


class WorkBuddyModelsError(ValueError):
    """Supplier model list request failed."""


def parse_supplier_models(payload) -> list[dict]:
    models: list[dict] = []
    seen: set[str] = set()
    for row in _model_rows(payload):
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not isinstance(mid, str):
            continue
        mid = mid.strip()
        if not mid or mid in seen:
            continue
        if _has_non_chat_tag(row):
            continue
        seen.add(mid)
        name = str(row.get("name") or row.get("display_name") or mid)
        item = {"id": mid, "name": name or mid}
        item.update(capacity_fields(row))
        item.update(reasoning_fields(row))
        # Cost/scheduling metadata: keep missing values unknown, never coerce to 0.
        item["credits"] = _credits_value(row)
        if "credits" in row:
            item["credits_raw"] = row.get("credits")
        for key in _COST_FIELDS:
            if key in row:
                item[key] = row[key]
        if "reasoning" in row:
            item["reasoning_raw"] = row["reasoning"]
        description = str(row.get("description") or "")
        if description:
            item["description"] = description
        models.append(item)
    return models


def _model_rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rows = data.get("models")
        if isinstance(rows, list):
            return rows
    rows = payload.get("models")
    return rows if isinstance(rows, list) else []


def _has_non_chat_tag(row: dict) -> bool:
    tags = row.get("tags")
    if not isinstance(tags, list):
        return False
    return any(str(tag) in _NON_CHAT_TAGS for tag in tags)


async def fetch_supplier_models(account: dict) -> list[dict]:
    headers = await auth_manager.get_billing_headers(account)
    if not headers:
        raise WorkBuddyModelsError("no usable WorkBuddy token")
    headers = dict(headers)
    headers.pop("Content-Type", None)
    headers["Accept"] = "application/json"
    url = f"{auth_manager.backend_url(account)}{MODELS_PATH}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        raise WorkBuddyModelsError(f"models HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WorkBuddyModelsError("models response is not JSON") from exc
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0):
            raise WorkBuddyModelsError(str(payload.get("msg") or payload.get("message") or code))
    return parse_supplier_models(payload)
