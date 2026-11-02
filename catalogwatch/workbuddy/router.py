"""Cost/credit-aware model and account selection for the pool gateway."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..util import utcnow_iso
from .catalog import WorkBuddyCatalog
from .load import LoadTracker
from .model_filter import is_text_chat_model, is_tool_capable_model
from .schedule import (
    DEFAULT_DAY_PRIORITY,
    DEFAULT_NIGHT_PRIORITY,
    is_full_price,
    is_night,
    night_window,
    priority_rank,
)
from .store import WorkBuddyStore

ALIASES = {"auto", "free", "cheap"}
# The only model sources this platform serves: WorkBuddy CN and INTL.
BRIDGE_PROVIDERS = {"workbuddy", "workbuddy-i"}

DEFAULT_POLICY: dict[str, Any] = {
    "prefer_free": True,
    "allow_night": True,
    "max_multiplier": 0,
    "allow_over_max": False,
    "min_credit_reserve": 0,
    "account_strategy": "expiring_first",
    # balanced: alternate auto tasks across active CN/INTL accounts by account
    # count; cost_first: always use the cheapest provider/model.
    "account_sharing": "balanced",
    # Automatic scheduling must never call base-price models.
    "deny_full_price": True,
    # One extra attempt: provider catalogs can list entries the account cannot
    # serve; a bad pick should not sink the whole alias request.
    "max_attempts": 4,
    "max_model_candidates": 4,
    "free_429_threshold": 3,
    "unknown_cost": "after_known",
    "routing_mode": "balanced",      # balanced | stable | cost
    "stability_weight": 0.5,
    "load_weight": 0.2,
    "cost_weight": 0.2,
    "session_jitter": 0.1,
    "session_ttl_seconds": 1800,
    "night_auto": True,
    "night_start": "23:00",
    "night_end": "08:00",
    "day_priority": dict(DEFAULT_DAY_PRIORITY),
    "night_priority": dict(DEFAULT_NIGHT_PRIORITY),
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
    source: str = "workbuddy"


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
    def __init__(
        self,
        store: WorkBuddyStore,
        catalog: WorkBuddyCatalog,
        load: LoadTracker | None = None,
    ):
        self.store = store
        self.catalog = catalog
        self.load = load or LoadTracker()
        self._proven_at = 0.0
        self._proven: dict[tuple[str, str], int] = {}
        self._share_lock = threading.Lock()
        self._share_counts: dict[str, int] = {}

    def _balanced_sharing(self, policy: dict, intent: PoolIntent) -> bool:
        return (
            str(policy.get("account_sharing") or "balanced") == "balanced"
            and intent.model in {"auto", "free"}
        )

    def _active_provider_accounts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for account in self.store.list_accounts(present=True):
            if str(account.get("status") or "active") != "active":
                continue
            provider = str(account.get("provider") or "")
            if not provider:
                continue
            state = self.store.get_state(int(account["account_id"]))
            # Health errors from a previous outage clear only after a
            # successful request; keep those accounts in the share target so
            # they get traffic and can recover. A live cooldown still means
            # the account cannot serve right now.
            if _future(state.get("cooldown_until")):
                continue
            counts[provider] = counts.get(provider, 0) + 1
        return counts

    def _preferred_provider(self, models: list[dict]) -> str | None:
        active = self._active_provider_accounts()
        providers = [
            provider
            for provider in active
            if any(str(item.get("provider")) == provider for item in models)
        ]
        if len(providers) < 2:
            return None
        with self._share_lock:
            return min(
                providers,
                key=lambda provider: (
                    self._share_counts.get(provider, 0) / max(1, active[provider]),
                    self._share_counts.get(provider, 0),
                    provider,
                ),
            )

    def _note_share(self, provider: str) -> None:
        with self._share_lock:
            self._share_counts[provider] = self._share_counts.get(provider, 0) + 1

    def share_snapshot(self) -> dict:
        """Current fairness counters and the per-account target share."""
        active = self._active_provider_accounts()
        total = sum(active.values())
        with self._share_lock:
            counts = dict(self._share_counts)
        return {
            "providers": {
                provider: {
                    "active_accounts": accounts,
                    "requests": counts.get(provider, 0),
                    "requests_per_account": round(
                        counts.get(provider, 0) / max(1, accounts), 3
                    ),
                    "target_share": round(accounts / total, 3) if total else 0,
                }
                for provider, accounts in active.items()
            }
        }

    def _proven_models(self) -> dict[tuple[str, str], int]:
        """Recent success counts, cached briefly; the ledger survives restarts."""
        now = time.monotonic()
        if now - self._proven_at < 30.0:
            return self._proven
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(
            timespec="seconds"
        )
        try:
            self._proven = self.store.model_success_counts(since)
        except Exception:  # noqa: BLE001 - routing must survive a ledger hiccup
            self._proven = {}
        self._proven_at = now
        return self._proven

    @staticmethod
    def _wire_id(cost: dict) -> str:
        return f"{cost.get('provider')}/{cost.get('model_id')}"

    def _candidates(self) -> list[dict]:
        """Every callable model resource: WorkBuddy only."""
        costs: list[dict] = []
        if self.store.list_accounts(present=True):
            for item in self.catalog.list_costs():
                if not item.get("present", True):
                    continue
                item = dict(item)
                item["source"] = "workbuddy"
                costs.append(item)
        return costs

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
        if intent.tools_required and not is_tool_capable_model(
            cost.get("model_id"), cost.get("name")
        ):
            # Chat-capable but known to mangle tool names (diffusion models).
            return False
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
        # Wire ids are `<source>/<model id>`; only the bridge sources are valid
        # prefixes. A model id may itself contain slashes, so an unknown prefix
        # is not treated as a provider here.
        if "/" in requested:
            head, rest = requested.split("/", 1)
            if head in BRIDGE_PROVIDERS:
                provider, model_id = head, rest

        # 显式点名伪模型/视频/音频/图像模型时直接拒绝：池不代理非文本能力，
        # 交给上游也只会得到一个难懂的协议错误。
        if not is_text_chat_model(model_id):
            raise PoolError(
                "model_not_text",
                f"Model '{requested}' is not a text chat model; the pool only serves text models",
                400,
            )

        matches = [
            item
            for item in costs
            if item.get("model_id") == model_id
            and (provider is None or item.get("provider") == provider)
        ]
        if matches:
            return matches

        # Unprefixed ids containing a slash are legacy direct-provider syntax
        # (nvidia/..., groq/...). They must exist in the WorkBuddy catalog to be
        # accepted; otherwise fail loudly instead of forwarding an unusable id.
        if provider is None and "/" in model_id:
            raise PoolError(
                "model_unavailable",
                f"Model '{requested}' is not a WorkBuddy model; this platform serves only "
                "WorkBuddy-bridged models",
                404,
            )

        # Explicit WorkBuddy models may be newer than our catalog. Preserve the
        # request and let the bridge return its own error if the model is invalid.
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
                "source": "workbuddy",
            }
            for item in providers
        ]

    def _rank_models(
        self,
        intent: PoolIntent,
        key: dict,
        policy: dict,
        *,
        session_id: str | None = None,
    ) -> list[dict]:
        costs = self._candidates()
        if not costs:
            if not self.store.list_accounts(present=True):
                raise PoolError(
                    "no_accounts",
                    "No WorkBuddy account is logged in",
                    503,
                )
            raise PoolError("no_models", "No callable model is available yet", 503)

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
            provider = str(item.get("provider"))
            model_id = str(item.get("model_id"))
            wire_id = self._wire_id(item)
            if self.load.cooling(provider, model_id):
                # A provider may list a catalog entry it cannot serve for this
                # account ("Function not found"); do not keep picking it.
                continue
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

        if bool(policy.get("deny_full_price", True)):
            # Automatic scheduling may only use free or currently discounted
            # models. Explicit model IDs still follow the request.
            filtered = [item for item in filtered if not is_full_price(item)]
            if not filtered:
                raise PoolError(
                    "no_discounted_models",
                    "No free or discounted WorkBuddy model is available right now",
                    503,
                )

        priorities = policy.get("model_priority") or {}
        prefer_free = bool(policy.get("prefer_free", True))
        allow_night = bool(policy.get("allow_night", True))
        routing_mode = str(policy.get("routing_mode") or "balanced")
        stability_weight = float(policy.get("stability_weight") or 0.5)
        load_weight = float(policy.get("load_weight") or 0.2)
        cost_weight = float(policy.get("cost_weight") or 0.2)
        jitter_weight = float(policy.get("session_jitter") or 0.1)
        if routing_mode == "stable":
            # Stability-first preset; use max_multiplier/free tier to bound cost.
            stability_weight = max(stability_weight, 0.6)
            cost_weight = 0.0

        proven = self._proven_models()
        window_start, window_end, _window_source = night_window(policy, costs)
        current_period = (
            "night"
            if allow_night
            and is_night(datetime.now(timezone.utc), window_start, window_end)
            else "day"
        )

        def key_for(item: dict):
            credits = item.get("credits")
            effective = float(credits) if credits is not None else math.inf
            if intent.model == "cheap":
                # `cheap` is the explicit lowest-multiplier alias; it must not
                # be reordered by the day/night priority map.
                tier = 0
            else:
                tier = priority_rank(
                    item, current_period, policy, allow_night=allow_night
                )
            # Within a cost tier, prefer models that have actually answered
            # recently; provider catalogs list entries the account cannot use.
            proven_rank = (
                0
                if proven.get(
                    (str(item.get("provider")), str(item.get("model_id"))), 0
                )
                > 0
                else 1
            )
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
            if routing_mode == "cost":
                # Cost-first after the free tier: the current effective
                # multiplier (promotions/night windows included) wins; recent
                # success only breaks ties.
                return (
                    tier,
                    effective,
                    proven_rank,
                    night_rank,
                    -quality,
                    str(item.get("model_id")),
                    str(item.get("provider")),
                )

            provider = str(item.get("provider"))
            model_id = str(item.get("model_id"))
            stability = self.load.stability(provider, model_id)
            inflight = self.load.inflight(provider, model_id)
            load_score = 1.0 / (1.0 + inflight)
            cost_score = (
                1.0 / (1.0 + max(0.0, effective)) if math.isfinite(effective) else 0.0
            )
            jitter = self.load.jitter(session_id, provider, model_id)
            score = (
                stability_weight * stability
                + load_weight * load_score
                + cost_weight * cost_score
                + jitter_weight * jitter
            )
            # Free first, then the current effective multiplier (promotions
            # and night windows included). Stability/load/jitter only break
            # cost ties; they must never override a cheaper model.
            return (
                tier,
                effective,
                night_rank,
                -score,
                proven_rank,
                -quality,
                model_id,
                provider,
            )

        filtered.sort(key=key_for)
        limit = max(1, min(10, int(policy.get("max_model_candidates") or 4)))
        if self._balanced_sharing(policy, intent):
            # Keep the best candidates from every provider so the provider
            # fairness pass can choose either side without losing its models.
            grouped: dict[str, list[dict]] = {}
            for item in filtered:
                grouped.setdefault(str(item.get("provider")), []).append(item)
            result: list[dict] = []
            for items in grouped.values():
                result.extend(items[:limit])
            return result
        return filtered[:limit]

    def _accounts_for(
        self,
        provider: str,
        cost: dict,
        policy: dict,
        *,
        preferred_account_id: int | None = None,
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

        def preferred_flag(account: dict) -> int:
            if preferred_account_id is None:
                return 1
            return 0 if int(account["account_id"]) == int(preferred_account_id) else 1

        def account_load(entry) -> int:
            account = entry[0]
            return self.load.account_load(provider, int(account["account_id"]))

        def free_key(entry):
            account, state, cap = entry
            used = self.store.account_model_usage_today(
                int(account["account_id"]), model_id, day
            ).get("requests", 0)
            headroom = (cap - int(used)) if cap else 10**9
            # Idle/least-used first: an agent task should spread across the
            # account pool, not stay on whichever account answered last.
            return (
                preferred_flag(account),
                account_load(entry),
                int(state.get("daily_requests") or 0),
                -int(account.get("priority") or 0),
                -int(account.get("weight") or 1),
                -int(headroom),
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
                credit_tiebreak = (-available, -expiring)
            elif strategy == "expiring_first":
                credit_tiebreak = (-expiring, -available)
            else:
                credit_tiebreak = (-available, -expiring)
            return (
                preferred_flag(account),
                account_load(entry),
                int(state.get("daily_requests") or 0),
                *credit_tiebreak,
                -int(account.get("priority") or 0),
                -int(account.get("weight") or 1),
                int(account["account_id"]),
            )

        result.sort(key=free_key if free else paid_key)
        return result

    def _bound_cost(
        self,
        binding: dict,
        intent: PoolIntent,
        key: dict,
        policy: dict,
    ) -> dict | None:
        provider = str(binding.get("provider") or "")
        model_id = str(binding.get("model_id") or "")
        if not provider or not model_id:
            return None
        cost = self.catalog.effective_cost(provider, model_id)
        if cost is None or not cost.get("present", True):
            return None
        if self.load.cooling(provider, model_id):
            return None
        if not self._capable(cost, intent):
            return None
        if not self._model_allowed(model_id, f"{provider}/{model_id}", key, policy):
            return None
        credits = cost.get("credits")
        max_multiplier = float(policy.get("max_multiplier") or 0)
        if (
            max_multiplier > 0
            and credits is not None
            and float(credits) > max_multiplier
            and not policy.get("allow_over_max")
        ):
            return None
        if credits is None and str(policy.get("unknown_cost") or "after_known") == "never":
            return None
        return cost

    def plan(
        self,
        intent: PoolIntent,
        key: dict,
        *,
        session_id: str | None = None,
    ) -> list[Attempt]:
        policy = self.policy()
        if key.get("max_attempts"):
            policy["max_attempts"] = key["max_attempts"]

        binding = None
        if session_id and intent.model in ALIASES:
            binding = self.store.get_session(session_id)

        models = self._rank_models(
            intent, key, policy, session_id=session_id
        )
        preferred_account_id: int | None = None
        binding_valid = False
        if binding:
            bound = self._bound_cost(binding, intent, key, policy)
            if bound is not None:
                # A session that once fell back to a paid model must not stay
                # pinned there after a free model becomes usable again. The
                # user's policy is free-first: the cheap affinity is worth
                # breaking, while a free binding keeps session cache affinity.
                keep_binding = not (
                    intent.model in ALIASES
                    and not bound.get("free")
                    and bool(policy.get("prefer_free", True))
                    and any(item.get("free") for item in models)
                )
                if keep_binding:
                    binding_valid = True
                    models = [
                        bound,
                        *[
                            item
                            for item in models
                            if not (
                                item.get("provider") == bound.get("provider")
                                and item.get("model_id") == bound.get("model_id")
                            )
                        ],
                    ]
                    try:
                        preferred_account_id = (
                            int(binding.get("account_id"))
                            if binding.get("account_id") is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        preferred_account_id = None
            else:
                self.store.delete_session(session_id)

        if not binding_valid and self._balanced_sharing(policy, intent):
            preferred = self._preferred_provider(models)
            if preferred:
                preferred_models = [
                    item for item in models if str(item.get("provider")) == preferred
                ]
                other_models = [
                    item for item in models if str(item.get("provider")) != preferred
                ]
                models = []
                # Preferred provider first, but interleave the other side so a
                # failed preferred attempt still has a fallback within
                # max_attempts.
                while preferred_models or other_models:
                    if preferred_models:
                        models.append(preferred_models.pop(0))
                    if other_models:
                        models.append(other_models.pop(0))

        max_attempts = max(1, min(5, int(policy.get("max_attempts") or 3)))
        used_pairs: set[tuple[int, str]] = set()
        attempts: list[Attempt] = []

        for cost in models:
            provider = str(cost.get("provider"))
            model_id = str(cost.get("model_id"))
            wire_id = self._wire_id(cost)
            accounts = self._accounts_for(
                provider,
                cost,
                policy,
                preferred_account_id=preferred_account_id,
            )
            if not accounts:
                continue
            for account, state, _cap in accounts:
                account_id = int(account["account_id"])
                pair = (account_id, model_id)
                if pair in used_pairs:
                    continue
                used_pairs.add(pair)
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
                        source="workbuddy",
                    )
                )
                if len(attempts) >= max_attempts:
                    break
            if len(attempts) >= max_attempts:
                break

        if not attempts:
            raise PoolError(
                "no_accounts",
                "No WorkBuddy account can serve this request"
                " (login/credits/cooldown/caps are all unavailable)",
                503,
            )

        if self._balanced_sharing(policy, intent):
            self._note_share(str(attempts[0].provider))

        if session_id and intent.model in ALIASES:
            first = attempts[0]
            try:
                ttl = int(policy.get("session_ttl_seconds") or 1800)
            except (TypeError, ValueError):
                ttl = 1800
            self.store.upsert_session(
                session_id,
                provider=first.provider,
                model_id=first.model_id,
                account_id=(
                    int(first.account["account_id"]) if first.account is not None else None
                ),
                ttl_seconds=ttl,
            )
        return attempts
