from __future__ import annotations

import httpx

from catalogwatch.adapters import (
    GoogleModelsAdapter,
    OpenAIModelsAdapter,
    OpenRouterAdapter,
    WorkBuddyAdapter,
)
from catalogwatch.config import ProviderConfig
from catalogwatch.models import ModelRecord


def _cfg(**overrides) -> ProviderConfig:
    values = {
        "id": "test",
        "name": "Test",
        "adapter": "openai_models",
        "base_url": "https://example.com/v1",
        "free_kind": "free_tier",
    }
    values.update(overrides)
    return ProviderConfig(**values)


def test_openrouter_zero_price_is_free():
    adapter = OpenRouterAdapter(_cfg(free_kind="zero_price"), httpx.AsyncClient(), None)

    free = adapter.normalize(
        {
            "id": "vendor/model:free",
            "name": "Vendor Model (free)",
            "pricing": {"prompt": "0", "completion": "0"},
            "context_length": 262144,
            "top_provider": {"max_completion_tokens": 8192},
        }
    )
    assert free is not None
    assert free.is_free is True
    assert free.free_kind == "zero_price"
    assert free.context_length == 262144
    assert free.max_output == 8192

    paid = adapter.normalize(
        {"id": "vendor/model", "pricing": {"prompt": "0.0000003", "completion": "0.000001"}}
    )
    assert paid is not None
    assert paid.is_free is False
    assert paid.free_kind == "paid"


def test_openai_compatible_free_tier_and_context_fallback():
    adapter = OpenAIModelsAdapter(_cfg(), httpx.AsyncClient(), None)
    record = adapter.normalize(
        {"id": "llama-3.3-70b", "context_window": 128000, "max_output_tokens": 4096}
    )
    assert record is not None
    assert record.is_free is True
    assert record.free_kind == "free_tier"
    assert record.context_length == 128000
    assert record.max_output == 4096


def test_workbuddy_adapter_dedupes_prefixed_duplicates():
    records = [
        ModelRecord(provider_id="workbuddy", model_id="glm-5.2", name="GLM"),
        ModelRecord(provider_id="workbuddy", model_id="workbuddy/glm-5.2", name="GLM"),
        ModelRecord(provider_id="workbuddy", model_id="workbuddy/only-prefixed", name="X"),
        ModelRecord(provider_id="workbuddy", model_id="workbuddy-i/global-only", name="G"),
    ]
    deduped = WorkBuddyAdapter.dedupe(records)
    assert [record.model_id for record in deduped] == [
        "glm-5.2",
        "workbuddy/only-prefixed",
        "workbuddy-i/global-only",
    ]


def test_google_normalize():
    adapter = GoogleModelsAdapter(
        _cfg(adapter="google_models", auth="query"), httpx.AsyncClient(), None
    )
    record = adapter.normalize(
        {
            "name": "models/gemini-2.0-flash",
            "displayName": "Gemini 2.0 Flash",
            "inputTokenLimit": 1048576,
            "outputTokenLimit": 8192,
            "supportedGenerationMethods": ["generateContent"],
        }
    )
    assert record is not None
    assert record.model_id == "gemini-2.0-flash"
    assert record.context_length == 1048576
    assert record.max_output == 8192
    assert record.is_free is True
