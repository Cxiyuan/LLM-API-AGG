from __future__ import annotations

from typing import Any

import httpx

from ..models import ModelRecord
from ..util import as_int
from .base import AdapterError, BaseAdapter


class GoogleModelsAdapter(BaseAdapter):
    """Google Generative Language API (Gemini). Auth is passed as ?key=..."""

    async def fetch_models(self) -> list[ModelRecord]:
        self.ensure_key()
        headers, params = self.request_parts()
        url = f"{self.cfg.base_url}/models"

        records: list[ModelRecord] = []
        page_token: str | None = None
        for _ in range(20):
            request_params = dict(params)
            if page_token:
                request_params["pageToken"] = page_token
            try:
                response = await self.client.get(url, headers=headers, params=request_params)
            except httpx.HTTPError as exc:
                raise AdapterError(f"request failed: {exc}") from exc

            self.check_response(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise AdapterError("response is not JSON") from exc

            models = payload.get("models")
            if models is None:
                models = []
            if not isinstance(models, list):
                raise AdapterError("unexpected model list shape (expected 'models')")

            for item in models:
                if isinstance(item, dict):
                    record = self.normalize(item)
                    if record is not None:
                        records.append(record)

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return records

    def normalize(self, item: dict[str, Any]) -> ModelRecord | None:
        raw_name = item.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            return None
        model_id = raw_name.rsplit("/", 1)[-1]
        methods = item.get("supportedGenerationMethods")
        if isinstance(methods, list) and methods:
            if not any(method in {"generateContent", "embedContent", "predict"} for method in methods):
                return None
        return ModelRecord(
            provider_id=self.cfg.id,
            model_id=model_id,
            name=str(item.get("displayName") or model_id),
            context_length=as_int(item.get("inputTokenLimit")),
            max_output=as_int(item.get("outputTokenLimit")),
            is_free=True,
            free_kind="free_tier",
            pricing=None,
            raw=item,
        )
