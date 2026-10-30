from __future__ import annotations

from .openai_compat import OpenAIModelsAdapter


class OpenRouterAdapter(OpenAIModelsAdapter):
    """OpenRouter's public model list embeds pricing, so zero-price == free.

    The parent implementation handles the `free_kind: zero_price` semantics; this
    subclass exists so the config can name the provider explicitly and so future
    OpenRouter-specific quirks (e.g. per-request fees) have a home.
    """
