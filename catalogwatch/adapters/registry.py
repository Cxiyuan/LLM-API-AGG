from __future__ import annotations

import httpx

from ..config import ProviderConfig
from .base import AdapterError, BaseAdapter
from .google import GoogleModelsAdapter
from .openai_compat import OpenAIModelsAdapter
from .openrouter import OpenRouterAdapter
from .workbuddy import WorkBuddyAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    "openai_models": OpenAIModelsAdapter,
    "openrouter": OpenRouterAdapter,
    "google_models": GoogleModelsAdapter,
    "workbuddy": WorkBuddyAdapter,
}


def build_adapter(
    cfg: ProviderConfig, client: httpx.AsyncClient, api_key: str | None
) -> BaseAdapter:
    adapter_cls = ADAPTERS.get(cfg.adapter)
    if adapter_cls is None:
        raise AdapterError(
            f"unknown adapter {cfg.adapter!r}; available: {', '.join(sorted(ADAPTERS))}"
        )
    return adapter_cls(cfg, client, api_key)
