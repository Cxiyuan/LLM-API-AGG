"""Unified WorkBuddy pool gateway.

External agents authenticate with a pool key. The gateway chooses a model and a
strictly pinned account, forwards to Buddy2api, and records the attempt ledger.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

import httpx

from ..util import utcnow_iso
from .bridge import BridgeError, BuddyBridge
from .router import Attempt, PoolError, PoolIntent, PoolRouter, extract_intent
from .store import WorkBuddyStore


class PoolHTTPError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict:
    return {"error": {"message": message, "type": "pool_error", "code": code}}


def _retryable(status: int) -> bool:
    return status in {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}


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
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "credit": None}
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens") or (prompt + completion)
    credit = usage.get("credit")
    if credit is None and "credit" in body:
        credit = body.get("credit")
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
        "credit": credit,
        "finish_reason": finish,
    }


def _routing(attempts: list[Attempt]) -> list[dict]:
    return [
        {
            "account_id": item.account.get("account_id"),
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
        limit = int(key.get("daily_request_limit") or 0)
        if not self.store.reserve_key_request(int(key_id), day, limit):
            raise PoolHTTPError(429, "key_rate_limited", "Pool key daily request limit exceeded")

    def _start(self, request_id: str, key: dict, endpoint: str, intent: PoolIntent, attempts: list[Attempt]) -> None:
        self.store.start_request(
            request_id=request_id,
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

    @staticmethod
    def _attempt_headers(attempt: Attempt, request_id: str, attempt_no: int) -> dict[str, str]:
        return {
            "X-WorkBuddy-Account": str(attempt.account.get("account_id")),
            "X-WorkBuddy-Account-Mode": "strict",
            "X-Buddy-Client-Request-Id": f"{request_id}:{attempt_no}",
        }

    @staticmethod
    def _response_headers(attempt: Attempt, request_id: str, attempt_no: int, cost_source: str) -> dict[str, str]:
        credits = attempt.effective_credits
        return {
            "X-Pool-Request-Id": request_id,
            "X-Pool-Model": attempt.model_id,
            "X-Pool-Provider": attempt.provider,
            "X-Pool-Account": str(attempt.account.get("account_id")),
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
    ) -> None:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        credit = usage.get("credit")
        credit_value = float(credit) if credit is not None else 0.0
        self.store.record_attempt(
            request_id=request_id,
            attempt_no=attempt_no,
            account_id=int(attempt.account["account_id"]),
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
            bridge_account_id=int(attempt.account["account_id"]),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
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
        self.store.increment_daily(
            int(attempt.account["account_id"]),
            free=attempt.free,
            tokens=total,
            credits=credit_value,
        )
        self.store.note_success(int(attempt.account["account_id"]))

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
    ) -> None:
        account_id = int(attempt.account["account_id"])
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
            self.store.set_cooldown(account_id, _cooldown(60), message)
        elif status_code in {401, 403}:
            self.store.note_failure(account_id, status_code, message, cooldown_until=_cooldown(300))
        elif status_code >= 500:
            self.store.note_failure(account_id, status_code, message, cooldown_until=_cooldown(15))
        else:
            self.store.note_failure(account_id, status_code, message, cooldown_until=_cooldown(30))

    # ------------------------------------------------------------------ non-stream

    async def chat(
        self, payload: dict, key: dict, *, endpoint: str = "chat"
    ) -> tuple[int, dict, dict[str, str]]:
        intent = extract_intent(payload)
        attempts = self.router.plan(intent, key)
        self._reserve(key)
        request_id = "wb-" + secrets.token_hex(12)
        self._start(request_id, key, endpoint, intent, attempts)
        last_error: BridgeError | None = None

        for attempt_no, attempt in enumerate(attempts, start=1):
            body = dict(payload)
            body["model"] = attempt.wire_id
            headers = self._attempt_headers(attempt, request_id, attempt_no)
            started = time.monotonic()
            try:
                if endpoint == "responses":
                    response = await self.bridge.post_responses_with_headers(body, headers)
                else:
                    response = await self.bridge.post_chat_with_headers(body, headers)
                result = response.json()
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
                )
                if _retryable(exc.status_code) and attempt_no < len(attempts):
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
            )
            headers_out = self._response_headers(
                attempt, request_id, attempt_no, str(attempt.cost.get("cost_source") or "unknown")
            )
            return response.status_code, result, headers_out

        detail = str(last_error) if last_error else "No WorkBuddy attempt succeeded"
        raise PoolHTTPError(503, "no_attempt_succeeded", detail)

    # --------------------------------------------------------------------- stream

    async def open_stream(
        self, payload: dict, key: dict, *, endpoint: str = "chat"
    ) -> tuple[httpx.Response, Attempt, str, int, dict[str, str], int, dict]:
        intent = extract_intent(payload)
        attempts = self.router.plan(intent, key)
        self._reserve(key)
        request_id = "wb-" + secrets.token_hex(12)
        self._start(request_id, key, endpoint, intent, attempts)
        last_error: BridgeError | None = None

        for attempt_no, attempt in enumerate(attempts, start=1):
            body = dict(payload)
            body["model"] = attempt.wire_id
            headers = self._attempt_headers(attempt, request_id, attempt_no)
            started = time.monotonic()
            try:
                if endpoint == "responses":
                    response = await self.bridge.open_responses_stream(body, headers)
                else:
                    response = await self.bridge.open_chat_stream(body, headers)
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
                )
                if _retryable(exc.status_code) and attempt_no < len(attempts):
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
                )
                if _retryable(response.status_code) and attempt_no < len(attempts):
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

    async def stream_body(
        self,
        response: httpx.Response,
        attempt: Attempt,
        request_id: str,
        attempt_no: int,
        key: dict,
        started: float,
    ) -> AsyncIterator[bytes]:
        buffer = b""
        last_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "credit": None, "finish_reason": None}
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
                buffer += chunk
                if len(buffer) > 256_000:
                    buffer = buffer[-128_000:]
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.strip()
                    if not text.startswith(b"data:"):
                        continue
                    payload = text[5:].strip()
                    if not payload or payload == b"[DONE]":
                        continue
                    try:
                        value = json.loads(payload)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(value, dict) and isinstance(value.get("usage"), dict):
                        merged = _usage(value)
                        if merged.get("total_tokens") or merged.get("credit") is not None:
                            last_usage = merged
            latency = int((time.monotonic() - started) * 1000)
            self._success(
                attempt=attempt,
                request_id=request_id,
                attempt_no=attempt_no,
                key=key,
                usage=last_usage,
                status_code=response.status_code,
                latency_ms=latency,
            )
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
