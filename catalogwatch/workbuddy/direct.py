"""Direct inference for Free-LLM-API catalog providers.

WorkBuddy is one source among many. Every configured provider that exposes an
OpenAI-compatible ``/chat/completions`` endpoint (OpenRouter, Groq, Cerebras,
Mistral, Cohere, SambaNova, NVIDIA, Z.ai, Gemini's compatibility layer) can
serve its **free** catalog models directly with the user's own key.

Only free models are exposed: the platform's promise is zero-cost usage, and a
paid model reached through ``auto`` would spend money silently. Paid models
stay visible in the unified inventory as catalog-only.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from ..config import ProviderConfig, Settings
from ..util import as_int
from .model_filter import is_text_chat_model

# Provider ids owned by the pool (bridge) rather than a direct HTTP endpoint.
BRIDGE_PROVIDERS = {"workbuddy", "workbuddy-i"}

# Cooldown windows after a failed direct attempt.
COOLDOWN_RATE_LIMIT = 60
COOLDOWN_SERVER = 15
COOLDOWN_AUTH = 300
# A provider can list a model it does not serve for this account ("Function
# not found"); stop retrying that exact model for a while.
COOLDOWN_MODEL_UNAVAILABLE = 1800

# OpenAI Chat Completions fields forwarded to a direct provider. Pool-specific
# metadata (`session_id`, routing hints) must never leak into a provider request.
DIRECT_BODY_KEYS = frozenset({
    "model", "messages", "tools", "tool_choice", "temperature", "top_p",
    "max_tokens", "max_completion_tokens", "stop", "presence_penalty",
    "frequency_penalty", "n", "response_format", "seed", "user",
    "reasoning_effort", "parallel_tool_calls", "logprobs", "top_logprobs",
    "stream", "stream_options",
})


class DirectError(RuntimeError):
    """A direct provider refused or failed the chat request."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def provider_key(cfg: ProviderConfig) -> str | None:
    value = os.environ.get(cfg.key_env or "", "").strip()
    return value or None


class DirectSource:
    """Callable non-bridge providers plus their free text-model candidates."""

    def __init__(
        self,
        settings: Settings,
        store: Any,
        client: httpx.AsyncClient | None = None,
        key_store: Any = None,
    ):
        self.settings = settings
        self.store = store
        self.key_store = key_store
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=20.0),
            follow_redirects=False,
        )
        self._cooldowns: dict[tuple[str, str], float] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------- provider facts

    def configs(self) -> dict[str, ProviderConfig]:
        return {
            cfg.id: cfg
            for cfg in self.settings.providers
            if cfg.id not in BRIDGE_PROVIDERS and cfg.enabled and cfg.chat_enabled
        }

    def known(self, provider_id: str) -> bool:
        return provider_id in self.configs()

    def config(self, provider_id: str) -> ProviderConfig | None:
        return self.configs().get(provider_id)

    def key(self, provider_id: str) -> str | None:
        cfg = self.config(provider_id)
        if cfg is None:
            return None
        if self.key_store is not None:
            return self.key_store.resolve(cfg)
        return provider_key(cfg)

    def provider_ready(self, provider_id: str) -> bool:
        return self.key(provider_id) is not None

    def chat_url(self, cfg: ProviderConfig) -> str:
        base = (cfg.chat_base_url or cfg.base_url).rstrip("/")
        return f"{base}/chat/completions"

    # ---------------------------------------------------------------- cooldowns

    def _cooling(self, key: tuple[str, str]) -> bool:
        until = self._cooldowns.get(key)
        if until is None:
            return False
        if until <= time.monotonic():
            self._cooldowns.pop(key, None)
            return False
        return True

    def cooling(self, provider_id: str, model_id: str) -> bool:
        return self._cooling((provider_id, "*")) or self._cooling((provider_id, model_id))

    def mark_cooldown(self, provider_id: str, model_id: str, status_code: int) -> None:
        if status_code == 429:
            seconds, key = COOLDOWN_RATE_LIMIT, (provider_id, model_id)
        elif status_code == 404:
            seconds, key = COOLDOWN_MODEL_UNAVAILABLE, (provider_id, model_id)
        elif status_code in {401, 403}:
            # A bad key is provider-wide; no point hammering the same endpoint.
            seconds, key = COOLDOWN_AUTH, (provider_id, "*")
        elif status_code >= 500 or status_code == 0:
            seconds, key = COOLDOWN_SERVER, (provider_id, model_id)
        else:
            return
        self._cooldowns[key] = time.monotonic() + seconds

    # ---------------------------------------------------------------- candidates

    @staticmethod
    def _candidate(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "direct",
            "provider": row["provider_id"],
            "model_id": row["model_id"],
            "name": row.get("name") or row["model_id"],
            "context_length": as_int(row.get("context_length")),
            "max_output": as_int(row.get("max_output")),
            # The catalog does not disclose tool/vision capability per model;
            # unknown is permissive and the upstream rejects what it cannot do.
            "capabilities": {},
            "base_credits": 0.0,
            "credits": 0.0,
            "free": True,
            "badge": None,
            "badge_color": None,
            "promotion_active": False,
            "promotion_factor": None,
            "promotion_until": "",
            "promotion_text_zh": "",
            "promotion_text_en": "",
            "cost_source": "free",
            "promotions": [],
            "present": True,
            "is_free": True,
        }

    def candidates(self) -> list[dict[str, Any]]:
        ready = {pid for pid in self.configs() if self.provider_ready(pid)}
        if not ready:
            return []
        rows = self.store.list_catalog_models(present=True, free=True)
        result: list[dict[str, Any]] = []
        for row in rows:
            provider_id = str(row.get("provider_id") or "")
            if provider_id not in ready:
                continue
            model_id = str(row.get("model_id") or "")
            if not is_text_chat_model(model_id, row.get("name")):
                continue
            if self.cooling(provider_id, model_id):
                continue
            result.append(self._candidate(row))
        return result

    def cost_of(self, provider_id: str, model_id: str) -> dict[str, Any] | None:
        if provider_id not in self.configs() or not self.provider_ready(provider_id):
            return None
        if self.cooling(provider_id, model_id):
            return None
        for row in self.store.list_catalog_models(present=True, free=True):
            if row.get("provider_id") == provider_id and row.get("model_id") == model_id:
                if not is_text_chat_model(model_id, row.get("name")):
                    return None
                return self._candidate(row)
        return None

    def is_paid(self, provider_id: str, model_id: str) -> bool:
        for row in self.store.list_catalog_models(present=True, free=False):
            if row.get("provider_id") == provider_id and row.get("model_id") == model_id:
                return True
        return False

    # ------------------------------------------------------------------ transport

    def _request_parts(
        self, cfg: ProviderConfig, key: str, *, stream: bool
    ) -> tuple[dict[str, str], dict[str, str]]:
        auth = (cfg.chat_auth or cfg.auth or "bearer").lower()
        param = cfg.chat_auth_param or cfg.auth_param or "key"
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
        }
        params: dict[str, str] = {}
        if auth == "bearer":
            headers["authorization"] = f"Bearer {key}"
        elif auth == "header":
            headers[param] = key
        elif auth == "query":
            params[param] = key
        return headers, params

    def _prepare(
        self, provider_id: str, payload: dict[str, Any], *, stream: bool
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, Any]]:
        cfg = self.config(provider_id)
        if cfg is None:
            raise DirectError(503, f"provider '{provider_id}' is not available for inference")
        key = self.key(provider_id)
        if key is None:
            raise DirectError(503, f"provider '{provider_id}' has no API key configured")
        headers, params = self._request_parts(cfg, key, stream=stream)
        body = {key_: value for key_, value in payload.items() if key_ in DIRECT_BODY_KEYS}
        body["stream"] = stream
        # `stream_options` is optional in the OpenAI spec and some
        # compatibility layers reject unknown fields; usage is recorded when
        # the provider sends it, and free providers cost 0 regardless.
        body.pop("stream_options", None)
        return self.chat_url(cfg), headers, params, body

    async def post_chat(self, provider_id: str, payload: dict[str, Any]) -> httpx.Response:
        url, headers, params, body = self._prepare(provider_id, payload, stream=False)
        try:
            response = await self.client.post(url, json=body, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise DirectError(0, f"transport: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:500].replace("\n", " ")
            raise DirectError(response.status_code, detail or f"HTTP {response.status_code}")
        return response

    async def open_chat(self, provider_id: str, payload: dict[str, Any]) -> httpx.Response:
        url, headers, params, body = self._prepare(provider_id, payload, stream=True)
        request = self.client.build_request(
            "POST", url, json=body, headers=headers, params=params
        )
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise DirectError(0, f"transport: {exc}") from exc
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
            await response.aclose()
            raise DirectError(
                response.status_code, detail or f"HTTP {response.status_code}"
            )
        return response
