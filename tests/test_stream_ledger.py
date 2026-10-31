"""Streaming ledger outcomes: in-stream errors and client cancellations."""

from __future__ import annotations

import asyncio
import time

import pytest

from catalogwatch.db import Database
from catalogwatch.workbuddy.catalog import WorkBuddyCatalog
from catalogwatch.workbuddy.gateway import PoolGateway, _channel_rejected
from catalogwatch.workbuddy.router import Attempt, PoolRouter
from catalogwatch.workbuddy.store import WorkBuddyStore


class _FakeStream:
    status_code = 200

    def __init__(self, parts):
        self.parts = parts

    async def aiter_bytes(self):
        for part in self.parts:
            yield part

    async def aclose(self):
        return None


def _setup(tmp_path):
    db = Database(tmp_path / "stream.db")
    store = WorkBuddyStore(db)
    router = PoolRouter(store, WorkBuddyCatalog(store, None))
    store.start_request(
        request_id="r1",
        pool_key_id=None,
        endpoint="chat",
        model_requested="m",
        stream=True,
        intent={},
        routing=[],
        session_id=None,
    )
    return store, PoolGateway(store, router, bridge=None)


def _attempt() -> Attempt:
    return Attempt(
        provider="workbuddy",
        model_id="m",
        wire_id="workbuddy/m",
        cost={},
        account=None,
        account_state={},
        free=True,
        effective_credits=0.0,
    )


async def test_stream_error_event_marks_request_failed(tmp_path):
    store, gateway = _setup(tmp_path)
    response = _FakeStream([
        b'data: {"error":{"message":"quota exceeded"}}\n\n',
        b"data: [DONE]\n\n",
    ])
    body = [
        chunk
        async for chunk in gateway.stream_body(
            response, _attempt(), "r1", 1, {}, time.monotonic()
        )
    ]
    assert body  # bytes still reached the client
    row = store.conn.execute(
        "SELECT status_code, error_code FROM pool_requests WHERE request_id='r1'"
    ).fetchone()
    assert row["status_code"] == 502
    assert row["error_code"] == "stream_error"
    attempt = store.conn.execute(
        "SELECT status_code, error_code FROM pool_request_attempts WHERE request_id='r1'"
    ).fetchone()
    assert attempt["status_code"] == 502


def test_channel_rejection_messages_are_retryable():
    assert _channel_rejected("11128 upstream_channel rejected")
    assert _channel_rejected("UPSTREAM_CHANNEL")
    assert not _channel_rejected("rate limit exceeded")


async def test_truncated_stream_is_not_a_success(tmp_path):
    store, gateway = _setup(tmp_path)
    response = _FakeStream([
        b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    ])
    body = [
        chunk
        async for chunk in gateway.stream_body(
            response, _attempt(), "r1", 1, {}, time.monotonic()
        )
    ]
    assert body
    row = store.conn.execute(
        "SELECT status_code, error_code FROM pool_requests WHERE request_id='r1'"
    ).fetchone()
    assert row["status_code"] == 502
    assert row["error_code"] == "stream_incomplete"


async def test_client_cancellation_is_recorded_without_penalty(tmp_path):
    store, gateway = _setup(tmp_path)

    class _CancelStream(_FakeStream):
        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise asyncio.CancelledError()

    response = _CancelStream([])
    with pytest.raises(asyncio.CancelledError):
        async for _chunk in gateway.stream_body(
            response, _attempt(), "r1", 1, {}, time.monotonic()
        ):
            pass
    row = store.conn.execute(
        "SELECT status_code, error_code FROM pool_requests WHERE request_id='r1'"
    ).fetchone()
    assert row["status_code"] == 499
    assert row["error_code"] == "client_canceled"
