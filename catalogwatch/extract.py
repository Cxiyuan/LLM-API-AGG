from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import ExtractorConfig, resolve_env

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You extract free-tier quota facts from LLM provider documentation.
Return ONLY a JSON object, with no markdown fence and no commentary, using this shape:
{
  "provider": "string",
  "plans": [
    {
      "name": "string",
      "limits": [{"metric": "string", "value": number or string, "period": "string"}],
      "notes": "string"
    }
  ],
  "free_models": ["string"],
  "effective_date": "string or null",
  "source_url": "string"
}
Never invent numbers that do not appear in the page. If the page contains no quota
information, return {"provider": "...", "plans": [], "notes": "no quota information"}.
Keep the page's original language for names and notes."""


class LLMExtractor:
    def __init__(self, cfg: ExtractorConfig):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    @property
    def name(self) -> str:
        return f"llm:{self.cfg.model}"

    async def extract(self, provider_name: str, url: str, text: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        api_key = resolve_env(self.cfg.api_key_env)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        user_prompt = (
            f"Provider: {provider_name}\n"
            f"Source URL: {url}\n"
            f"Documentation text:\n\n{text[: self.cfg.max_chars]}"
        )
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": 900,
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                response = await client.post(
                    f"{self.cfg.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            if response.status_code >= 400:
                log.warning("quota extraction failed: HTTP %s", response.status_code)
                return None
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("quota extraction failed: %s", exc)
            return None

        if not isinstance(content, str):
            return None
        extracted = _parse_json_object(content)
        if extracted is None:
            log.warning("quota extraction returned non-JSON content")
        return extracted


def _parse_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
