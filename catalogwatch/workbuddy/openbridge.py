"""OpenBridge channel: a standalone OpenAI-compatible upstream.

OpenBridge is intentionally kept outside the Buddy2api account/catalog tables.
It exposes ``/health``, ``/v1/models`` and ``/v1/chat/completions``; the pool
treats its browser-backed quota as a quota-covered resource, not as WorkBuddy
credits.  The service may be absent, in which case this channel stays disabled.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .providers import OPENBRIDGE_PROVIDER
from .store import WorkBuddyStore


class OpenBridgeError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_models() -> list[str]:
    raw = (os.environ.get("OPENBRIDGE_MODELS") or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


class OpenBridgeClient:
    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = httpx.Timeout(timeout, connect=10.0, pool=30.0)
        self._transport = transport
        self._client = self._new_client()

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> Any:
        """Generic admin proxy used by the console.

        OpenBridge's provider/session APIs are plain JSON; keeping this in one
        place means the browser never receives the upstream auth key.
        """
        if not self.configured:
            raise OpenBridgeError(0, "OPENBRIDGE_URL is not configured")
        try:
            response = await self._client.request(
                method.upper(),
                path,
                headers=self._headers(),
                json=payload if payload is not None else None,
            )
        except httpx.HTTPError as exc:
            raise OpenBridgeError(0, f"OpenBridge request failed: {exc}") from exc
        if response.status_code >= 400:
            raise OpenBridgeError(response.status_code, response.text[:500])
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text[:2000]}

    async def health(self) -> dict:
        if not self.configured:
            raise OpenBridgeError(0, "OPENBRIDGE_URL is not configured")
        try:
            response = await self._client.get("/health", timeout=3.0)
        except httpx.HTTPError as exc:
            raise OpenBridgeError(0, f"OpenBridge health failed: {exc}") from exc
        if response.status_code >= 400:
            raise OpenBridgeError(response.status_code, response.text[:300])
        return response.json()

    async def models(self) -> list[dict]:
        """Fetch `/v1/models`, falling back to OPENBRIDGE_MODELS.

        Older OpenBridge builds only document the CLI inventory.  The model
        list is therefore optional; an explicit env list keeps routing usable
        when the HTTP endpoint is absent.
        """
        fallback = [
            {"id": model_id, "object": "model", "owned_by": OPENBRIDGE_PROVIDER}
            for model_id in _env_models()
        ]
        if not self.configured:
            return fallback
        try:
            response = await self._client.get("/v1/models", headers=self._headers())
        except httpx.HTTPError as exc:
            if fallback:
                return fallback
            raise OpenBridgeError(0, f"OpenBridge model list failed: {exc}") from exc
        if response.status_code >= 400:
            if fallback:
                return fallback
            raise OpenBridgeError(
                response.status_code, response.text[:300] or "OpenBridge model list failed"
            )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            return fallback
        return [item for item in data if isinstance(item, dict) and item.get("id")]

    async def post_chat(self, payload: dict) -> httpx.Response:
        return await self.post_chat_with_headers(payload, {})

    async def post_chat_with_headers(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        try:
            response = await self._client.post(
                "/v1/chat/completions",
                headers={**self._headers(), **(headers or {})},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise OpenBridgeError(0, f"OpenBridge chat failed: {exc}") from exc
        if response.status_code >= 400:
            raise OpenBridgeError(response.status_code, response.text[:500])
        return response

    async def open_chat_stream(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        request = self._client.build_request(
            "POST",
            "/v1/chat/completions",
            headers={**self._headers(), **(headers or {})},
            json=payload,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise OpenBridgeError(0, f"OpenBridge stream failed: {exc}") from exc
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
            await response.aclose()
            raise OpenBridgeError(response.status_code, detail)
        return response


class OpenBridgeCatalog:
    provider = OPENBRIDGE_PROVIDER

    def __init__(self, store: WorkBuddyStore, client: OpenBridgeClient):
        self.store = store
        self.client = client

    async def refresh(self) -> dict:
        if not self.client.configured:
            # Keep an explicit disabled entry so the console can show why the
            # channel has no models without treating it as an outage.
            self.store.upsert_models(self.provider, [], source_identity=self.provider)
            return {
                "provider": self.provider,
                "ok": True,
                "models": 0,
                "warning": "disabled",
            }
        warning = None
        try:
            raw = await self.client.models()
        except OpenBridgeError as exc:
            raw = []
            warning = str(exc)[:300]
        models: list[dict] = []
        for item in raw:
            model_id = str(item.get("id") or "").strip()
            if model_id.startswith(f"{self.provider}/"):
                model_id = model_id[len(self.provider) + 1 :]
            if not model_id:
                continue
            models.append(
                {
                    "id": model_id,
                    "name": str(item.get("name") or item.get("owned_by") or model_id),
                    # OpenBridge has no WorkBuddy credit metadata.  Its
                    # browser session quota is intentionally treated as a
                    # separate quota-covered resource.
                    "credits": 0.0,
                    "credits_raw": "quota",
                    "quota": True,
                    "supportsToolCall": _env_bool("OPENBRIDGE_SUPPORTS_TOOL_CALL", True),
                    "supportsReasoning": _env_bool("OPENBRIDGE_SUPPORTS_REASONING", True),
                    "supportsImages": _env_bool("OPENBRIDGE_SUPPORTS_IMAGES", False),
                    "context_length": int(os.environ.get("OPENBRIDGE_CONTEXT_WINDOW") or 128000),
                    "max_output": int(os.environ.get("OPENBRIDGE_MAX_OUTPUT") or 16384),
                }
            )
        self.store.upsert_models(self.provider, models, source_identity=self.provider)
        return {
            "provider": self.provider,
            "ok": warning is None,
            "models": len(models),
            "warning": warning,
        }
