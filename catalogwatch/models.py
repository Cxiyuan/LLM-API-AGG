from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ModelRecord:
    provider_id: str
    model_id: str
    name: str | None = None
    context_length: int | None = None
    max_output: int | None = None
    is_free: bool = True
    free_kind: str = "free_tier"
    pricing: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    def fingerprint(self) -> str:
        """Stable hash of the fields that should produce a change event.

        `raw` and `pricing` are excluded on purpose. Provider descriptions churn,
        and some OpenRouter aliases re-price by tiny amounts between requests. The
        price is still refreshed in the database; it only becomes an event when it
        flips `is_free` (which is part of this fingerprint).
        """
        payload = {
            "name": self.name,
            "context_length": self.context_length,
            "max_output": self.max_output,
            "is_free": self.is_free,
            "free_kind": self.free_kind,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def pricing_json(self) -> str | None:
        if not self.pricing:
            return None
        return json.dumps(self.pricing, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(slots=True)
class Event:
    kind: str
    summary: str
    provider_id: str | None = None
    subject: str | None = None
    payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "provider_id": self.provider_id,
            "subject": self.subject,
            "payload": self.payload,
        }
