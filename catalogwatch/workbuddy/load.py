"""In-process load and stability tracking for pool routing.

The pool runs one uvicorn worker and one asyncio loop, so an in-memory tracker
is sufficient and avoids a write per request. Long-term history remains in the
SQLite ledger; this tracker only answers "what is healthy and busy right now?".
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict


class LoadTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._model_inflight: dict[str, int] = defaultdict(int)
        self._account_inflight: dict[str, int] = defaultdict(int)
        self._stats: dict[str, dict] = {}
        # Short-lived "do not route here" marks for models that rejected a
        # request as unavailable (e.g. a provider catalog entry that is not
        # provisioned for this account). Kept in memory like the rest of the
        # live-routing state.
        self._cooldowns: dict[str, float] = {}

    @staticmethod
    def _model_key(provider: str, model_id: str) -> str:
        return f"{provider}/{model_id}"

    @staticmethod
    def _account_key(provider: str, model_id: str, account_id: int | None) -> str:
        return f"{provider}/{model_id}/{account_id}"

    def enter(self, provider: str, model_id: str, account_id: int | None = None) -> None:
        model_key = self._model_key(provider, model_id)
        account_key = self._account_key(provider, model_id, account_id)
        with self._lock:
            self._model_inflight[model_key] += 1
            self._account_inflight[account_key] += 1

    def exit(self, provider: str, model_id: str, account_id: int | None = None) -> None:
        model_key = self._model_key(provider, model_id)
        account_key = self._account_key(provider, model_id, account_id)
        with self._lock:
            self._model_inflight[model_key] = max(0, self._model_inflight[model_key] - 1)
            self._account_inflight[account_key] = max(
                0, self._account_inflight[account_key] - 1
            )

    def record(
        self,
        provider: str,
        model_id: str,
        account_id: int | None,
        *,
        success: bool,
        status_code: int | None,
        latency_ms: int,
    ) -> None:
        key = self._model_key(provider, model_id)
        with self._lock:
            stats = self._stats.setdefault(
                key,
                {"success": 0.5, "latency": 500.0, "rate_limit": 0.0, "total": 0},
            )
            target = 1.0 if success else 0.0
            stats["success"] += 0.1 * (target - stats["success"])
            stats["latency"] = 0.8 * stats["latency"] + 0.2 * max(0, int(latency_ms))
            rate_target = 1.0 if status_code == 429 else 0.0
            stats["rate_limit"] += 0.1 * (rate_target - stats["rate_limit"])
            stats["total"] += 1

    def cooldown(self, provider: str, model_id: str, seconds: float) -> None:
        """Stop routing to one model for a while (model-specific failures)."""
        key = self._model_key(provider, model_id)
        with self._lock:
            self._cooldowns[key] = time.monotonic() + max(1.0, float(seconds))

    def cooling(self, provider: str, model_id: str) -> bool:
        key = self._model_key(provider, model_id)
        with self._lock:
            until = self._cooldowns.get(key)
            if until is None:
                return False
            if until <= time.monotonic():
                self._cooldowns.pop(key, None)
                return False
            return True

    def stability(self, provider: str, model_id: str) -> float:
        key = self._model_key(provider, model_id)
        with self._lock:
            stats = self._stats.get(key)
            if not stats or not stats["total"]:
                return 0.5
            latency_score = 1.0 / (1.0 + float(stats["latency"]) / 1000.0)
            return (
                0.5 * float(stats["success"])
                + 0.3 * latency_score
                + 0.2 * (1.0 - float(stats["rate_limit"]))
            )

    def inflight(self, provider: str, model_id: str) -> int:
        with self._lock:
            return int(self._model_inflight.get(self._model_key(provider, model_id), 0))

    def account_inflight(
        self, provider: str, model_id: str, account_id: int | None
    ) -> int:
        with self._lock:
            return int(
                self._account_inflight.get(
                    self._account_key(provider, model_id, account_id), 0
                )
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "models": dict(self._model_inflight),
                "accounts": dict(self._account_inflight),
                "stats": {key: dict(value) for key, value in self._stats.items()},
            }

    @staticmethod
    def jitter(session_id: str | None, provider: str, model_id: str) -> float:
        token = (session_id or "anonymous").encode("utf-8")
        digest = hashlib.sha256(token + b"|" + f"{provider}/{model_id}".encode("utf-8")).hexdigest()
        return int(digest[:16], 16) / float(16**16)
