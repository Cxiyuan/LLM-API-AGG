"""Unified WorkBuddy pool gateway.

External agents authenticate with a pool key. The gateway chooses a model and a
strictly pinned account, forwards to Buddy2api, and records the attempt ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

import httpx

from ..util import utcnow_iso
from .bridge import BridgeError, BuddyBridge
from .router import ALIASES, Attempt, PoolError, PoolIntent, PoolRouter, extract_intent
from .store import WorkBuddyStore

log = logging.getLogger(__name__)


class PoolHTTPError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict:
    return {"error": {"message": message, "type": "pool_error", "code": code}}


def _context_retry(intent: PoolIntent, status: int, detail: object) -> bool:
    """Auto aliases may skip a model that returns a model-specific 400.

    The bridge uses 400 for per-model failures such as "context exceeded" and
    "model service info not found"; for auto/free/cheap these must fall through
    to the next candidate instead of ending the request.
    """
    return intent.model in ALIASES and status == 400


def _retryable(status: int) -> bool:
    # 0 = transport failure before an HTTP status reached us.
    # 404 = the catalog listed a model this account cannot serve; try another.
    return status in {0, 401, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504}


_CHANNEL_REJECTION_MARKERS = ("upstream_channel", "11128")


def _channel_rejected(message: str) -> bool:
    """WorkBuddy rejects some prompts per channel; another source may accept it."""
    text = str(message or "").lower()
    return any(marker in text for marker in _CHANNEL_REJECTION_MARKERS)


def _classify(status: int) -> str:
    if status in {401, 403}:
        return "auth_error"
    if status == 429:
        return "rate_limit"
    if status == 400:
        return "bad_request"
    if status >= 500:
        return "server_error"
    return "upstream_error"


def _error_status(error: object) -> int:
    """HTTP-like status carried by an in-stream error event, when present."""
    if not isinstance(error, dict):
        return 502
    raw = error.get("code")
    try:
        status = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        status = 0
    if 400 <= status <= 599:
        return status
    message = str(error.get("message") or "").lower()
    code = str(raw or "").lower()
    if "no available accounts" in message:
        return 503
    if "rate_limit" in code or "too many requests" in message or "429" in message:
        return 429
    if "context" in message or "上下文" in message:
        return 400
    return 502


def _account_cooling(store: WorkBuddyStore, account_id: int) -> bool:
    state = store.get_state(account_id)
    until = state.get("cooldown_until")
    if not until:
        return False
    try:
        moment = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment > datetime.now(timezone.utc)


def _cooldown(seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds")


def _next_reset() -> str:
    try:
        zone = ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001
        zone = timezone.utc
    local = datetime.now(zone)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat(timespec="seconds")


def _usage(body: Any) -> dict:
    if not isinstance(body, dict):
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "credit": None,
        }
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens") or (prompt + completion)
    credit = usage.get("credit")
    if credit is None and "credit" in body:
        credit = body.get("credit")
    details = usage.get("prompt_tokens_details")
    cache_hit_raw = (
        usage.get("prompt_cache_hit_tokens")
        if usage.get("prompt_cache_hit_tokens") is not None
        else usage.get("cache_read_input_tokens")
    )
    if cache_hit_raw is None and isinstance(details, dict):
        cache_hit_raw = details.get("cached_tokens")
    cache_miss_raw = (
        usage.get("prompt_cache_miss_tokens")
        if usage.get("prompt_cache_miss_tokens") is not None
        else usage.get("cache_creation_input_tokens")
    )
    try:
        prompt = int(prompt)
    except (TypeError, ValueError):
        prompt = 0
    try:
        completion = int(completion)
    except (TypeError, ValueError):
        completion = 0
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = prompt + completion
    try:
        credit = float(credit) if credit is not None else None
    except (TypeError, ValueError):
        credit = None
    try:
        cache_hit = max(0, int(cache_hit_raw or 0))
    except (TypeError, ValueError):
        cache_hit = 0
    try:
        cache_miss = max(0, int(cache_miss_raw or 0))
    except (TypeError, ValueError):
        cache_miss = 0
    if cache_hit and not cache_miss and prompt >= cache_hit:
        cache_miss = prompt - cache_hit
    finish = None
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    if finish is None and isinstance(body.get("incomplete_details"), dict):
        finish = body["incomplete_details"].get("reason")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "credit": credit,
        "finish_reason": finish,
    }


def _routing(attempts: list[Attempt]) -> list[dict]:
    return [
        {
            "source": item.source,
            "account_id": item.account.get("account_id") if item.account else None,
            "provider": item.provider,
            "model_id": item.model_id,
            "free": item.free,
            "credits": item.effective_credits,
        }
        for item in attempts
    ]


class PoolGateway:
    def __init__(
        self,
        store: WorkBuddyStore,
        router: PoolRouter,
        bridge: BuddyBridge,
    ):
        self.store = store
        self.router = router
        self.bridge = bridge

    # --------------------------------------------------------------- bookkeeping

    def _reserve(self, key: dict) -> None:
        key_id = key.get("id")
        if not key_id:
            return
        day = utcnow_iso()[:10]
        usage = self.store.key_usage_today(int(key_id), day)
        credit_limit = float(key.get("daily_credit_limit") or 0)
        if credit_limit > 0 and float(usage.get("credits") or 0) >= credit_limit:
            raise PoolHTTPError(
                429, "key_credit_limited", "Pool key daily credit limit exceeded"
            )
        limit = int(key.get("daily_request_limit") or 0)
        if not self.store.reserve_key_request(int(key_id), day, limit):
            raise PoolHTTPError(429, "key_rate_limited", "Pool key daily request limit exceeded")

    def _start(
        self,
        request_id: str,
        key: dict,
        endpoint: str,
        intent: PoolIntent,
        attempts: list[Attempt],
        *,
        session_id: str | None = None,
    ) -> None:
        self.store.start_request(
            request_id=request_id,
            session_id=session_id,
            pool_key_id=key.get("id"),
            endpoint=endpoint,
            model_requested=intent.model,
            stream=intent.stream,
            intent={
                "stream": intent.stream,
                "tools": intent.tools_required,
                "vision": intent.vision_required,
                "reasoning": intent.reasoning_required,
                "prompt_tokens": intent.estimated_prompt_tokens,
                "context_required": intent.context_required,
            },
            routing=_routing(attempts),
        )

    def _attempt_usable(self, intent: PoolIntent, attempt: Attempt) -> bool:
        """Skip candidates that failed earlier in this same request."""
        if intent.model in ALIASES and self.router.load.cooling(
            attempt.provider, attempt.model_id
        ):
            return False
        if attempt.account is not None and _account_cooling(
            self.store, int(attempt.account["account_id"])
        ):
            return False
        return True

    @staticmethod
    def _attempt_headers(attempt: Attempt, request_id: str, attempt_no: int) -> dict[str, str]:
        if attempt.account is None:
            return {}
        return {
            "X-WorkBuddy-Account": str(attempt.account.get("account_id")),
            "X-WorkBuddy-Account-Mode": "strict",
            "X-Buddy-Client-Request-Id": f"{request_id}:{attempt_no}",
        }

    @staticmethod
    def _response_headers(attempt: Attempt, request_id: str, attempt_no: int, cost_source: str) -> dict[str, str]:
        credits = attempt.effective_credits
        account_id = attempt.account.get("account_id") if attempt.account else ""
        return {
            "X-Pool-Request-Id": request_id,
            "X-Pool-Model": attempt.model_id,
            "X-Pool-Provider": attempt.provider,
            "X-Pool-Source": attempt.source,
            "X-Pool-Account": "" if account_id is None else str(account_id),
            "X-Pool-Effective-Credits": "" if credits is None else str(credits),
            "X-Pool-Free": "1" if attempt.free else "0",
            "X-Pool-Cost-Source": cost_source,
            "X-Pool-Attempts": str(attempt_no),
        }

    def _success(
        self,
        *,
        attempt: Attempt,
        request_id: str,
        attempt_no: int,
        key: dict,
        usage: dict,
        status_code: int,
        latency_ms: int,
        session_id: str | None = None,
        bind_session: bool = False,
    ) -> None:
        account_id = (
            int(attempt.account["account_id"]) if attempt.account is not None else None
        )
        self.router.load.exit(attempt.provider, attempt.model_id, account_id)
        self.router.load.record(
            attempt.provider,
            attempt.model_id,
            account_id,
            success=True,
            status_code=status_code,
            latency_ms=latency_ms,
        )
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        credit = usage.get("credit")
        credit_value = float(credit) if credit is not None else 0.0
        self.store.record_attempt(
            request_id=request_id,
            attempt_no=attempt_no,
            account_id=account_id,
            model_id=attempt.model_id,
            provider=attempt.provider,
            status_code=status_code,
            error_code=None,
            started_at=utcnow_iso(),
            duration_ms=latency_ms,
            credits=credit,
        )
        self.store.finish_request(
            request_id,
            model_routed=attempt.model_id,
            provider=attempt.provider,
            bridge_account_id=account_id,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cache_hit_tokens=int(usage.get("cache_hit_tokens") or 0),
            cache_miss_tokens=int(usage.get("cache_miss_tokens") or 0),
            credits=credit,
            credits_estimated=0,
            effective_credits=attempt.effective_credits,
            cost_source="upstream" if credit is not None else "pending",
            status_code=status_code,
            finish_reason=usage.get("finish_reason"),
            attempts=attempt_no,
            duration_ms=latency_ms,
            reconciled_at=None,
        )
        if key.get("id"):
            self.store.add_key_usage(
                int(key["id"]), utcnow_iso()[:10], tokens=total, credits=credit_value
            )
        if account_id is not None:
            self.store.increment_daily(
                account_id,
                free=attempt.free,
                tokens=total,
                credits=credit_value,
            )
            self.store.note_success(account_id)
        if session_id and bind_session:
            try:
                ttl = int(self.router.policy().get("session_ttl_seconds") or 1800)
            except (TypeError, ValueError):
                ttl = 1800
            self.store.upsert_session(
                session_id,
                provider=attempt.provider,
                model_id=attempt.model_id,
                account_id=account_id,
                ttl_seconds=ttl,
            )

    def _failure(
        self,
        *,
        attempt: Attempt,
        request_id: str,
        attempt_no: int,
        status_code: int,
        code: str,
        message: str,
        latency_ms: int,
        session_id: str | None = None,
    ) -> None:
        account_id = (
            int(attempt.account["account_id"]) if attempt.account is not None else None
        )
        self.router.load.exit(attempt.provider, attempt.model_id, account_id)
        self.router.load.record(
            attempt.provider,
            attempt.model_id,
            account_id,
            success=False,
            status_code=status_code,
            latency_ms=latency_ms,
        )
        self.store.record_attempt(
            request_id=request_id,
            attempt_no=attempt_no,
            account_id=account_id,
            model_id=attempt.model_id,
            provider=attempt.provider,
            status_code=status_code,
            error_code=code,
            started_at=utcnow_iso(),
            duration_ms=latency_ms,
            credits=None,
        )
        if status_code == 400:
            lowered = message.lower()
            model_missing = any(
                marker in lowered
                for marker in (
                    "service info not found",
                    "model unavailable",
                    "invalid model",
                    "model not found",
                )
            )
            context_bad = (
                "上下文超过可用预算" in message
                or "context_length_exceeded" in lowered
                or "context length" in lowered
            )
            if model_missing or context_bad:
                # Model-specific 400: avoid this model, but keep the account
                # available for other models.
                self.router.load.cooldown(
                    attempt.provider, attempt.model_id, 1800 if model_missing else 300
                )
                return
        if code in {
            "stream_error",
            "stream_incomplete",
            "stream_timeout",
            "stream_transport",
            "empty_response",
        }:
            # A stream that already started cannot be retried inside this
            # request. Cool the model (not the account) so the next auto pick
            # falls through to the next-cheapest model and retries the free one
            # after a short cooldown.
            self.router.load.cooldown(attempt.provider, attempt.model_id, 120)
            return
        if status_code == 404:
            # Model-specific unavailability: the catalog lists an entry this
            # account cannot serve ("Function ... not found"). Do not cool the
            # whole account down; avoid this model and let the retry move on.
            self.router.load.cooldown(attempt.provider, attempt.model_id, 1800)
            if session_id:
                binding = self.store.get_session(session_id)
                if (
                    binding is not None
                    and str(binding.get("provider")) == attempt.provider
                    and str(binding.get("model_id")) == attempt.model_id
                ):
                    self.store.delete_session(session_id)
            return
        if account_id is None:
            return
        if status_code == 429:
            state = self.store.get_state(account_id)
            count = int(state.get("free_429_count") or 0) + 1
            self.store.update_state(
                account_id,
                free_429_count=count,
                last_429_at=utcnow_iso(),
            )
            threshold = int(self.router.policy().get("free_429_threshold") or 3)
            if attempt.free and count >= threshold:
                self.store.set_free_exhausted(account_id, _next_reset())
            # WorkBuddy's 429s are model-scoped (daily model quota / model
            # saturation). Cool the model, not the account: another model on
            # the same account must remain routable.
            self.router.load.cooldown(attempt.provider, attempt.model_id, 300)
        elif status_code in {401, 403}:
            self.store.note_failure(account_id, status_code, message, cooldown_until=_cooldown(300))
        elif status_code >= 500:
            self.store.note_failure(account_id, status_code, message, cooldown_until=_cooldown(15))
        else:
            self.store.note_failure(account_id, status_code, message, cooldown_until=_cooldown(30))

    def _canceled(
        self,
        *,
        attempt: Attempt,
        request_id: str,
        attempt_no: int,
        latency_ms: int,
    ) -> None:
        """Client disconnected mid-stream: record it, but do not penalize the source."""
        account_id = (
            int(attempt.account["account_id"]) if attempt.account is not None else None
        )
        self.router.load.exit(attempt.provider, attempt.model_id, account_id)
        self.router.load.record(
            attempt.provider,
            attempt.model_id,
            account_id,
            success=False,
            status_code=499,
            latency_ms=latency_ms,
        )
        self.store.record_attempt(
            request_id=request_id,
            attempt_no=attempt_no,
            account_id=account_id,
            model_id=attempt.model_id,
            provider=attempt.provider,
            status_code=499,
            error_code="client_canceled",
            started_at=utcnow_iso(),
            duration_ms=latency_ms,
            credits=None,
        )
        self.store.finish_request(
            request_id,
            status_code=499,
            error_code="client_canceled",
            attempts=attempt_no,
            duration_ms=latency_ms,
        )

    # ------------------------------------------------------------------ non-stream

    async def chat(
        self,
        payload: dict,
        key: dict,
        *,
        endpoint: str = "chat",
        session_id: str | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        intent = extract_intent(payload)
        attempts = self.router.plan(
            intent, key, session_id=session_id
        )
        self._reserve(key)
        request_id = "wb-" + secrets.token_hex(12)
        self._start(
            request_id, key, endpoint, intent, attempts, session_id=session_id
        )
        bind_session = bool(session_id and intent.model in ALIASES)
        last_error: Exception | None = None

        for attempt_no, attempt in enumerate(attempts, start=1):
            if attempt_no > 1 and not self._attempt_usable(intent, attempt):
                continue
            body = dict(payload)
            body["model"] = attempt.wire_id
            headers = self._attempt_headers(attempt, request_id, attempt_no)
            self.router.load.enter(
                attempt.provider,
                attempt.model_id,
                int(attempt.account["account_id"]),
            )
            started = time.monotonic()
            try:
                if endpoint == "responses":
                    response = await self.bridge.post_responses_with_headers(body, headers)
                else:
                    response = await self.bridge.post_chat_with_headers(body, headers)
                result = response.json()
            except asyncio.CancelledError:
                latency = int((time.monotonic() - started) * 1000)
                self._canceled(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    latency_ms=latency,
                )
                raise
            except BridgeError as exc:
                last_error = exc
                latency = int((time.monotonic() - started) * 1000)
                self._failure(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    status_code=exc.status_code,
                    code=_classify(exc.status_code),
                    message=str(exc),
                    latency_ms=latency,
                    session_id=session_id,
                )
                retry = (
                    _retryable(exc.status_code)
                    or _channel_rejected(str(exc))
                    or _context_retry(intent, exc.status_code, exc)
                )
                if retry and attempt_no < len(attempts):
                    continue
                self.store.finish_request(
                    request_id,
                    status_code=exc.status_code,
                    error_code=_classify(exc.status_code),
                    attempts=attempt_no,
                    duration_ms=latency,
                )
                raise PoolHTTPError(exc.status_code or 502, _classify(exc.status_code), str(exc))
            except (ValueError, httpx.HTTPError) as exc:
                latency = int((time.monotonic() - started) * 1000)
                self._failure(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    status_code=502,
                    code="bad_upstream_response",
                    message=str(exc),
                    latency_ms=latency,
                    session_id=session_id,
                )
                if attempt_no < len(attempts):
                    continue
                self.store.finish_request(
                    request_id,
                    status_code=502,
                    error_code="bad_upstream_response",
                    attempts=attempt_no,
                    duration_ms=latency,
                )
                raise PoolHTTPError(502, "bad_upstream_response", str(exc))

            latency = int((time.monotonic() - started) * 1000)
            usage = _usage(result)
            self._success(
                attempt=attempt,
                request_id=request_id,
                attempt_no=attempt_no,
                key=key,
                usage=usage,
                status_code=response.status_code,
                latency_ms=latency,
                session_id=session_id,
                bind_session=bind_session,
            )
            headers_out = self._response_headers(
                attempt, request_id, attempt_no, str(attempt.cost.get("cost_source") or "unknown")
            )
            return response.status_code, result, headers_out

        detail = str(last_error) if last_error else "No WorkBuddy attempt succeeded"
        raise PoolHTTPError(503, "no_attempt_succeeded", detail)

    # --------------------------------------------------------------------- stream

    async def open_stream(
        self,
        payload: dict,
        key: dict,
        *,
        endpoint: str = "chat",
        session_id: str | None = None,
    ) -> tuple[httpx.Response, Attempt, str, int, dict[str, str], int, dict]:
        intent = extract_intent(payload)
        attempts = self.router.plan(
            intent, key, session_id=session_id
        )
        self._reserve(key)
        request_id = "wb-" + secrets.token_hex(12)
        self._start(
            request_id, key, endpoint, intent, attempts, session_id=session_id
        )
        last_error: Exception | None = None

        for attempt_no, attempt in enumerate(attempts, start=1):
            if attempt_no > 1 and not self._attempt_usable(intent, attempt):
                continue
            body = dict(payload)
            body["model"] = attempt.wire_id
            headers = self._attempt_headers(attempt, request_id, attempt_no)
            self.router.load.enter(
                attempt.provider,
                attempt.model_id,
                int(attempt.account["account_id"]),
            )
            started = time.monotonic()
            try:
                if endpoint == "responses":
                    response = await self.bridge.open_responses_stream(body, headers)
                else:
                    response = await self.bridge.open_chat_stream(body, headers)
            except asyncio.CancelledError:
                latency = int((time.monotonic() - started) * 1000)
                self._canceled(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    latency_ms=latency,
                )
                raise
            except BridgeError as exc:
                last_error = exc
                latency = int((time.monotonic() - started) * 1000)
                self._failure(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    status_code=exc.status_code,
                    code=_classify(exc.status_code),
                    message=str(exc),
                    latency_ms=latency,
                    session_id=session_id,
                )
                retry = (
                    _retryable(exc.status_code)
                    or _channel_rejected(str(exc))
                    or _context_retry(intent, exc.status_code, exc)
                )
                if retry and attempt_no < len(attempts):
                    continue
                self.store.finish_request(
                    request_id,
                    status_code=exc.status_code,
                    error_code=_classify(exc.status_code),
                    attempts=attempt_no,
                    duration_ms=latency,
                )
                raise PoolHTTPError(exc.status_code or 502, _classify(exc.status_code), str(exc))

            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
                await response.aclose()
                latency = int((time.monotonic() - started) * 1000)
                code = _classify(response.status_code)
                self._failure(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    status_code=response.status_code,
                    code=code,
                    message=detail,
                    latency_ms=latency,
                    session_id=session_id,
                )
                retry = (
                    _retryable(response.status_code)
                    or _channel_rejected(detail)
                    or _context_retry(intent, response.status_code, detail)
                )
                if retry and attempt_no < len(attempts):
                    continue
                self.store.finish_request(
                    request_id,
                    status_code=response.status_code,
                    error_code=code,
                    attempts=attempt_no,
                    duration_ms=latency,
                )
                raise PoolHTTPError(response.status_code, code, detail)

            headers_out = self._response_headers(
                attempt, request_id, attempt_no, str(attempt.cost.get("cost_source") or "unknown")
            )
            return response, attempt, request_id, attempt_no, headers_out, started, key

        detail = str(last_error) if last_error else "No WorkBuddy attempt succeeded"
        raise PoolHTTPError(503, "no_attempt_succeeded", detail)

    async def _stream_with_heartbeat(
        self, response: httpx.Response, interval: float = 15.0
    ) -> AsyncIterator[bytes]:
        """Forward upstream bytes; emit SSE comments while the upstream thinks.

        A long tool-calling turn can be silent for minutes. Intermediaries and
        client HTTP stacks (undici's body timeout is 300s) close an idle body;
        a `: keep-alive` comment keeps the connection alive without changing
        the event stream (SSE comment lines are ignored by parsers).
        """
        iterator = response.aiter_bytes().__aiter__()
        pending: asyncio.Future | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield b": keep-alive\n\n"
                    continue
                task, pending = pending, None
                try:
                    chunk = task.result()
                except StopAsyncIteration:
                    return
                yield chunk
        finally:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                else:
                    # The consumer may close the stream between the wait()
                    # and result(); retrieve the completed future so asyncio
                    # does not log "Task exception was never retrieved".
                    try:
                        pending.exception()
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass

    async def stream_body(
        self,
        response: httpx.Response,
        attempt: Attempt,
        request_id: str,
        attempt_no: int,
        key: dict,
        started: float,
        *,
        session_id: str | None = None,
        bind_session: bool = False,
    ) -> AsyncIterator[bytes]:
        buffer = b""
        last_usage: dict = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "credit": None,
            "finish_reason": None,
        }
        stream_error: Any = None
        saw_done = False
        saw_finish = False
        saw_content = False
        try:
            async for chunk in self._stream_with_heartbeat(response):
                # Parse before yielding: a client may stop after the last content
                # chunk, and a usage/finish event in that chunk must still land
                # in the ledger.
                buffer += chunk
                if len(buffer) > 256_000:
                    buffer = buffer[-128_000:]
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.strip()
                    if not text.startswith(b"data:"):
                        continue
                    payload = text[5:].strip()
                    if not payload:
                        continue
                    if payload == b"[DONE]":
                        saw_done = True
                        continue
                    try:
                        value = json.loads(payload)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(value, dict):
                        continue
                    choices = value.get("choices")
                    if isinstance(choices, list):
                        for choice in choices:
                            if not isinstance(choice, dict):
                                continue
                            if choice.get("finish_reason"):
                                saw_finish = True
                                last_usage["finish_reason"] = choice["finish_reason"]
                            delta = choice.get("delta")
                            if isinstance(delta, dict) and (
                                delta.get("content")
                                or delta.get("reasoning_content")
                                or delta.get("tool_calls")
                            ):
                                saw_content = True
                    if value.get("error"):
                        # Provider-level error delivered inside the stream. The
                        # client will see it too; record it so a 200-looking
                        # request is not mistaken for success.
                        stream_error = value.get("error")
                    if isinstance(value.get("usage"), dict):
                        merged = _usage(value)
                        if (
                            merged.get("total_tokens")
                            or merged.get("credit") is not None
                            or merged.get("cache_hit_tokens")
                        ):
                            last_usage = merged
                yield chunk
            latency = int((time.monotonic() - started) * 1000)
            if stream_error is not None:
                message = json.dumps(
                    stream_error, ensure_ascii=False, default=str
                )[:500]
                status = _error_status(stream_error)
                code = _classify(status) if status != 502 else "stream_error"
                log.warning(
                    "llm-agg: stream error (%s) for %s/%s: %s",
                    status,
                    attempt.provider,
                    attempt.model_id,
                    message,
                )
                self._failure(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    status_code=status,
                    code=code,
                    message=message,
                    latency_ms=latency,
                    session_id=session_id,
                )
                self.store.finish_request(
                    request_id,
                    status_code=status,
                    error_code=code,
                    attempts=attempt_no,
                    duration_ms=latency,
                )
                return
            if not saw_done and not saw_finish:
                # The upstream closed the body without a terminal marker. That
                # is a truncated stream, not a successful completion; clients
                # see the same cut-off, so record it for diagnosis.
                message = "upstream stream ended without [DONE] or finish_reason"
                log.warning(
                    "llm-agg: %s for %s/%s",
                    message,
                    attempt.provider,
                    attempt.model_id,
                )
                self._failure(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    status_code=502,
                    code="stream_incomplete",
                    message=message,
                    latency_ms=latency,
                    session_id=session_id,
                )
                self.store.finish_request(
                    request_id,
                    status_code=502,
                    error_code="stream_incomplete",
                    attempts=attempt_no,
                    duration_ms=latency,
                )
                return
            if not saw_content:
                # The stream finished normally but carried no text, reasoning
                # or tool call. Clients see an empty completion; recording it
                # as success makes auto keep choosing the same dead model.
                message = "upstream stream finished without any content"
                log.warning(
                    "llm-agg: %s for %s/%s",
                    message,
                    attempt.provider,
                    attempt.model_id,
                )
                self._failure(
                    attempt=attempt,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    status_code=502,
                    code="empty_response",
                    message=message,
                    latency_ms=latency,
                    session_id=session_id,
                )
                self.store.finish_request(
                    request_id,
                    status_code=502,
                    error_code="empty_response",
                    attempts=attempt_no,
                    duration_ms=latency,
                )
                return
            self._success(
                attempt=attempt,
                request_id=request_id,
                attempt_no=attempt_no,
                key=key,
                usage=last_usage,
                status_code=response.status_code,
                latency_ms=latency,
                session_id=session_id,
                bind_session=bind_session,
            )
        except asyncio.CancelledError:
            # The consumer (agent) went away mid-stream. Record a distinct
            # outcome instead of leaving the row empty; never cool the account.
            latency = int((time.monotonic() - started) * 1000)
            self._canceled(
                attempt=attempt,
                request_id=request_id,
                attempt_no=attempt_no,
                latency_ms=latency,
            )
            raise
        except httpx.HTTPError as exc:
            latency = int((time.monotonic() - started) * 1000)
            self._failure(
                attempt=attempt,
                request_id=request_id,
                attempt_no=attempt_no,
                status_code=502,
                code="stream_error",
                message=str(exc),
                latency_ms=latency,
                session_id=session_id,
            )
            self.store.finish_request(
                request_id,
                status_code=502,
                error_code="stream_error",
                attempts=attempt_no,
                duration_ms=latency,
            )
            raise
        finally:
            await response.aclose()
