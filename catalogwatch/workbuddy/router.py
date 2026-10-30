"""Cost/credit-aware model and account selection for the pool gateway."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..util import utcnow_iso
from .catalog import WorkBuddyCatalog
from .store import WorkBuddyStore

ALIASES = {"auto", "free", "cheap"}

DEFAULT_POLICY: dict[str, Any] = {
    "prefer_free": True,
    "allow_night": True,
    "max_multiplier": 0,
    "allow_over_max": False,
    "min_credit_reserve": 0,
    "account_strategy": "expiring_first",
    "max_attempts": 3,
    "max_model_candidates": 4,
    "free_429_threshold": 3,
    "unknown_cost": "after_known",
    "model_priority": {},
    "allow_models": [],
    "deny_models": [],
}


class PoolError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 503):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(slots=True)
class PoolIntent:
    model: str
    stream: bool
    tools_required: bool
    vision_required: bool
    reasoning_required: bool
    estimated_prompt_tokens: int
    requested_max_output: int | None
    context_required: int


@dataclass(slots=True)
class Attempt:
    provider: str
    model_id: str
    wire_id: str
    cost: dict
    account: dict
    account_state: dict
    free: bool
    effective_credits: float | None


def _future(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def _estimate_tokens(payload: dict) -> int:
    body = payload.get("messages")
    if body is None:
        body = payload.get("input")
    try:
        text = json.dumps(body, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(body)
    tokens = max(1, len(text) // 4)
    tools = payload.get("tools") or payload.get("functions")
    if tools:
        try:
            tokens += len(json.dumps(tools, ensure_ascii=False, default=str)) // 4
        except (TypeError, ValueError):
            pass
    return tokens + 64


def _contains_image(value: Any) -> bool:
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in {"image_url", "image", "input_image"}:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


def extract_intent(payload: dict) -> PoolIntent:
    model = str(payload.get("model") or "auto").strip()
    stream = bool(payload.get("stream"))
    tools = payload.get("tools") or payload.get("functions")
    vision = _contains_image(payload.get("messages")) or _contains_image(payload.get("input"))
    reasoning = bool(payload.get("reasoning") or payload.get("reasoning_effort"))
    prompt = _estimate_tokens(payload)
    max_output = (
        payload.get("max_tokens")
        or payload.get("max_completion_tokens")
        or payload.get("max_output_tokens")
    )
    try:
        max_output = int(max_output) if max_output is not None else None
    except (TypeError, ValueError):
        max_output = None
    return PoolIntent(
        model=model,
        stream=stream,
        tools_required=bool(tools),
        vision_required=vision,
        reasoning_required=reasoning,
        estimated_prompt_tokens=prompt,
        requested_max_output=max_output,
        context_required=prompt + (max_output or 0) + 512,
    )


class PoolRouter:
    def __init__(self, store: WorkBuddyStore, catalog: WorkBuddyCatalog):
        self.store = store
        self.catalog = catalog

    def policy(self) -> dict:
        value = self.store.get_setting("policy", {})
        merged = dict(DEFAULT_POLICY)
        if isinstance(value, dict):
            merged.update(value)
        return merged

    def _model_allowed(self, model_id: str, wire_id: str, key: dict, policy: dict) -> bool:
        allowed = key.get("allowed_models")
        if isinstance(allowed, list) and allowed:
            names = {str(item) for item in allowed}
            if model_id not in names and wire_id not in names:
                return False
        allow = policy.get("allow_models") or []
        if allow and model_id not in {str(item) for item in allow}:
            return False
        deny = policy.get("deny_models") or []
        if model_id in {str(item) for item in deny}:
            return False
        return True

    @staticmethod
    def _capable(cost: dict, intent: PoolIntent) -> bool:
        caps = cost.get("capabilities") or {}
        if intent.tools_required and caps.get("supportsToolCall") is False:
            return False
        if intent.vision_required and caps.get("supportsImages") is not True:
            return False
        if intent.reasoning_required and caps.get("supportsReasoning") is False:
            return False
        context = cost.get("context_length")
        if context and intent.context_required > int(context):
            return False
        return True

    def _explicit_candidates(self, intent: PoolIntent, costs: list[dict]) -> list[dict]:
        requested = intent.model
        provider = None
        model_id = requested
        if requested.startswith("workbuddy-i/"):
            provider = "workbuddy-i"
            model_id = requested[len("workbuddy-i/") :]
        elif requested.startswith("workbuddy/"):
            provider = "workbuddy"
            model_id = requested[len("workbuddy/") :]

        matches = [
            item
            for item in costs
            if item.get("model_id") == model_id
            and (provider is None or item.get("provider") == provider)
        ]
        if matches:
            return matches

        # Explicit models may be newer than our catalog. Preserve the request
        # and let the bridge return its own error if the model is invalid.
        providers = [provider] if provider else ["workbuddy", "workbuddy-i"]
        return [
            {
                "provider": item,
                "model_id": model_id,
                "name": model_id,
                "credits": None,
                "free": False,
                "capabilities": {},
                "context_length": None,
                "max_output": None,
                "promotion_active": False,
                "promotion_factor": None,
                "cost_source": "unknown",
            }
            for item in providers
        ]

    def _rank_models(self, intent: PoolIntent, key: dict, policy: dict) -> list[dict]:
        if not self.store.list_accounts(present=True):
            raise PoolError(
                "no_accounts",
                "No WorkBuddy account is logged in to the pool",
                503,
            )
        costs = [item for item in self.catalog.list_costs() if item.get("present", True)]
        if not costs:
            raise PoolError("no_models", "No WorkBuddy models are available yet", 503)

        explicit = intent.model not in ALIASES
        if explicit:
            candidates = self._explicit_candidates(intent, costs)
            allowed = [
                item
                for item in candidates
                if self._model_allowed(
                    item["model_id"],
                    f"{item['provider']}/{item['model_id']}",
                    key,
                    policy,
                )
            ]
            if not allowed:
                raise PoolError(
                    "model_denied",
                    "This pool key may not use the requested model",
                    403,
                )
            return allowed

        filtered = []
        for item in costs:
            wire_id = f"{item['provider']}/{item['model_id']}"
            if not self._model_allowed(item["model_id"], wire_id, key, policy):
                continue
            if not self._capable(item, intent):
                continue
            if intent.model == "free" and not item.get("free"):
                continue
            credits = item.get("credits")
            max_multiplier = float(policy.get("max_multiplier") or 0)
            if (
                max_multiplier > 0
                and credits is not None
                and float(credits) > max_multiplier
                and not policy.get("allow_over_max")
            ):
                continue
            filtered.append(item)

        if not filtered:
            reason = "no_free_models" if intent.model == "free" else "no_capable_models"
            raise PoolError(reason, "No model satisfies the request and policy", 503)

        unknown_policy = str(policy.get("unknown_cost") or "after_known")
        known = [item for item in filtered if item.get("credits") is not None]
        if unknown_policy == "never":
            filtered = known
        elif unknown_policy == "after_known" and known:
            filtered = known
        if not filtered:
            raise PoolError(
                "no_priced_models",
                "No model has a usable cost estimate under the current policy",
                503,
            )

        priorities = policy.get("model_priority") or {}
        prefer_free = bool(policy.get("prefer_free", True))
        allow_night = bool(policy.get("allow_night", True))

        def key_for(item: dict):
            credits = item.get("credits")
            effective = float(credits) if credits is not None else math.inf
            free_rank = 0 if (item.get("free") and prefer_free) else 1
            night_rank = (
                0
                if (
                    allow_night
                    and item.get("promotion_active")
                    and item.get("promotion_factor") is not None
                    and float(item["promotion_factor"]) < 1
                )
                else 1
            )
            caps = item.get("capabilities") or {}
            try:
                priority = float(priorities.get(item["model_id"], 0) or 0)
            except (TypeError, ValueError):
                priority = 0.0
            quality = (
                (100 if caps.get("supportsReasoning") else 0)
                + (1 if caps.get("supportsImages") else 0)
                + min(1_000_000, int(item.get("context_length") or 0)) / 1_000_000
                + priority
            )
            return (
                free_rank,
                effective,
                night_rank,
                -quality,
                str(item.get("model_id")),
                str(item.get("provider")),
            )

        filtered.sort(key=key_for)
        limit = max(1, min(10, int(policy.get("max_model_candidates") or 4)))
        return filtered[:limit]

    def _accounts_for(
        self, provider: str, cost: dict, policy: dict
    ) -> list[tuple[dict, dict, int | None]]:
        day = utcnow_iso()[:10]
        model_id = str(cost["model_id"])
        free = bool(cost.get("free"))
        min_reserve = float(policy.get("min_credit_reserve") or 0)
        result: list[tuple[dict, dict, int | None]] = []
        strategy = str(policy.get("account_strategy") or "expiring_first")
        for account in self.store.list_accounts(present=True):
            if str(account.get("provider")) != provider:
                continue
            if str(account.get("status") or "active") != "active":
                continue
            state = self.store.get_state(int(account["account_id"]))
            if _future(state.get("cooldown_until")):
                continue
            if free and _future(state.get("free_exhausted_until")):
                continue
            caps = self.store.get_caps(int(account["account_id"]), model_id)
            usage = self.store.account_model_usage_today(
                int(account["account_id"]), model_id, day
            )
            account_usage = self.store.account_usage_today(int(account["account_id"]), day)
            request_cap = int(caps.get("daily_request_cap") or 0)
            if request_cap > 0 and (
                int(usage.get("requests") or 0) >= request_cap
                or int(account_usage.get("requests") or 0) >= request_cap
            ):
                continue
            credit_cap = float(caps.get("daily_credit_cap") or 0)
            if credit_cap > 0 and (
                float(usage.get("credits") or 0) >= credit_cap
                or float(account_usage.get("credits") or 0) >= credit_cap
            ):
                continue
            available = account.get("available_credits")
            try:
                available = float(available) if available is not None else None
            except (TypeError, ValueError):
                available = None
            if not free and available is not None and available <= min_reserve:
                continue
            result.append((account, state, int(request_cap) if request_cap > 0 else None))

        def free_key(entry):
            account, state, cap = entry
            used = self.store.account_model_usage_today(
                int(account["account_id"]), model_id, day
            ).get("requests", 0)
            headroom = (cap - int(used)) if cap else 10**9
            return (
                -int(headroom),
                -int(account.get("priority") or 0),
                -int(account.get("weight") or 1),
                int(state.get("daily_requests") or 0),
                int(account["account_id"]),
            )

        def paid_key(entry):
            account, state, _cap = entry
            available = account.get("available_credits")
            try:
                available = float(available) if available is not None else 0.0
            except (TypeError, ValueError):
                available = 0.0
            expiring = account.get("credit_expiring_30d") or 0
            try:
                expiring = float(expiring)
            except (TypeError, ValueError):
                expiring = 0.0
            if strategy == "richest_first":
                return (
                    -available,
                    -int(account.get("priority") or 0),
                    -int(account.get("weight") or 1),
                    int(state.get("daily_requests") or 0),
                    int(account["account_id"]),
                )
            return (
                -expiring,
                -available,
                -int(account.get("priority") or 0),
                -int(account.get("weight") or 1),
                int(state.get("daily_requests") or 0),
                int(account["account_id"]),
            )

        result.sort(key=free_key if free else paid_key)
        return result

    def plan(self, intent: PoolIntent, key: dict) -> list[Attempt]:
        policy = self.policy()
        if key.get("max_attempts"):
            policy["max_attempts"] = key["max_attempts"]
        models = self._rank_models(intent, key, policy)
        max_attempts = max(1, min(5, int(policy.get("max_attempts") or 3)))
        used_accounts: set[int] = set()
        attempts: list[Attempt] = []

        for cost in models:
            provider = str(cost.get("provider"))
            model_id = str(cost.get("model_id"))
            wire_id = f"{provider}/{model_id}"
            accounts = self._accounts_for(provider, cost, policy)
            if not accounts:
                continue
            for account, state, _cap in accounts:
                account_id = int(account["account_id"])
                if account_id in used_accounts:
                    continue
                used_accounts.add(account_id)
                attempts.append(
                    Attempt(
                        provider=provider,
                        model_id=model_id,
                        wire_id=wire_id,
                        cost=cost,
                        account=account,
                        account_state=state,
                        free=bool(cost.get("free")),
                        effective_credits=cost.get("credits"),
                    )
                )
                if len(attempts) >= max_attempts:
                    return attempts

        if not attempts:
            raise PoolError(
                "no_accounts",
                "No WorkBuddy account can serve this request (login, credits, cooldown, or caps)",
                503,
            )
        return attempts
