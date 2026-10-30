"""Reconcile pending pool requests with Buddy2api's request logs.

The gateway records usage from the upstream response when available. Some
responses omit `usage.credit`; those rows are left as `cost_source='pending'`
and filled here using the `client_request_id` we sent to the bridge.
"""

from __future__ import annotations

import logging

from .bridge import BridgeError, BuddyBridge
from .store import WorkBuddyStore

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, store: WorkBuddyStore, bridge: BuddyBridge):
        self.store = store
        self.bridge = bridge

    async def reconcile(
        self, *, limit: int = 50, older_than_seconds: int = 60
    ) -> dict:
        rows = self.store.pending_requests(limit=limit, older_than_seconds=older_than_seconds)
        checked = reconciled = unknown = bridge_failures = 0
        for row in rows:
            checked += 1
            request_id = str(row.get("request_id") or "")
            if not request_id:
                continue
            attempts = max(1, int(row.get("attempts") or 1))
            found = None
            failed = False
            for attempt_no in range(1, attempts + 1):
                try:
                    result = await self.bridge.search_logs(
                        client_request_id=f"{request_id}:{attempt_no}", limit=5
                    )
                except BridgeError as exc:
                    failed = True
                    log.warning("reconcile %s failed to query bridge: %s", request_id, exc)
                    break
                items = result.get("items") if isinstance(result, dict) else []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        status = int(item.get("status_code") or 0)
                    except (TypeError, ValueError):
                        status = 0
                    if 200 <= status < 300:
                        found = item
                        break
                if found:
                    break

            if failed:
                bridge_failures += 1
                continue
            if not found:
                self.store.mark_reconcile_unknown(request_id)
                unknown += 1
                continue

            try:
                prompt = int(found.get("prompt_tokens") or 0)
            except (TypeError, ValueError):
                prompt = 0
            try:
                completion = int(found.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                completion = 0
            try:
                total = int(found.get("total_tokens") or 0)
            except (TypeError, ValueError):
                total = 0
            if not total:
                total = prompt + completion
            raw_credit = found.get("credit")
            try:
                credits = float(raw_credit) if raw_credit is not None else 0.0
            except (TypeError, ValueError):
                credits = 0.0
            try:
                status = int(found.get("status_code") or 200)
            except (TypeError, ValueError):
                status = 200
            self.store.reconcile_request(
                request_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                credits=credits,
                status_code=status,
                finish_reason=found.get("finish_reason"),
            )
            reconciled += 1

        return {
            "checked": checked,
            "reconciled": reconciled,
            "unknown": unknown,
            "bridge_failures": bridge_failures,
        }
