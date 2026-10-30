from __future__ import annotations

from typing import Any

import httpx

from ..models import ModelRecord
from ..util import as_int, first_present
from .base import AdapterError, BaseAdapter


def _is_zero(value: Any) -> bool:
    if value is None:
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return str(value).strip() in {"0", "0.0", "0.00"}


class OpenAIModelsAdapter(BaseAdapter):
    """Adapter for providers exposing an OpenAI-compatible GET /models."""

    async def fetch_models(self) -> list[ModelRecord]:
        self.ensure_key()
        headers, params = self.request_parts()
        url = f"{self.cfg.base_url}/{self.cfg.models_path.lstrip('/')}"
        try:
            response = await self.client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise AdapterError(f"request failed: {exc}") from exc

        self.check_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError("response is not JSON") from exc

        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise AdapterError("unexpected model list shape (expected a list under 'data')")

        records: list[ModelRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            record = self.normalize(item)
            if record is not None:
                records.append(record)
        return records

    def normalize(self, item: dict[str, Any]) -> ModelRecord | None:
        model_id = item.get("id") or item.get("name")
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        model_id = model_id.strip()

        top_provider = item.get("top_provider")
        if not isinstance(top_provider, dict):
            top_provider = {}

        context_length = as_int(
            first_present(
                item,
                "context_length",
                "context_window",
                "max_context_length",
                "max_model_len",
                "max_input_tokens",
            )
        )
        max_output = as_int(
            first_present(item, "max_completion_tokens", "max_output_tokens", "max_tokens")
        )
        if max_output is None:
            max_output = as_int(top_provider.get("max_completion_tokens"))

        pricing = item.get("pricing")
        if not isinstance(pricing, dict):
            pricing = None

        is_free, free_kind = self.free_status(pricing)
        return ModelRecord(
            provider_id=self.cfg.id,
            model_id=model_id,
            name=str(item.get("name") or model_id),
            context_length=context_length,
            max_output=max_output,
            is_free=is_free,
            free_kind=free_kind,
            pricing=pricing,
            raw=item,
        )

    def free_status(self, pricing: dict[str, Any] | None) -> tuple[bool, str]:
        kind = self.cfg.free_kind
        if kind == "zero_price":
            if pricing and _is_zero(pricing.get("prompt")) and _is_zero(pricing.get("completion")):
                return True, "zero_price"
            return False, "paid"
        # free_tier / credits mean "usable within the provider's free allowance".
        return True, kind
