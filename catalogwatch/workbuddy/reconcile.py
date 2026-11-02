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
            prompt = completion = total = 0
            credits = 0.0
            found = False
            failed = False
            last_status = input_status = row.get("status_code")
            try:
                input_status = int(input_status) if input_status is not None else None
            except (TypeError, ValueError):
                input_status = None
            last_finish = row.get("finish_reason")
            for attempt_no in range(1, attempts + 1):
                try:
                    result = await self.bridge.search_logs(
                        client_request_id=f"{request_id}:{attempt_no}", limit=20
                    )
                except BridgeError as exc:
                    failed = True
                    log.warning("reconcile %s failed to query bridge: %s", request_id, exc)
                    break
                items = result.get("items") if isinstance(result, dict) else []
                if not isinstance(items, list):
                    continue
                attempt_credits = 0.0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    found = True
                    try:
                        prompt += int(item.get("prompt_tokens") or 0)
                    except (TypeError, ValueError):
                        pass
                    try:
                        completion += int(item.get("completion_tokens") or 0)
                    except (TypeError, ValueError):
                        pass
                    try:
                        item_total = int(item.get("total_tokens") or 0)
                    except (TypeError, ValueError):
                        item_total = 0
                    if not item_total:
                        try:
                            item_total = int(item.get("prompt_tokens") or 0) + int(
                                item.get("completion_tokens") or 0
                            )
                        except (TypeError, ValueError):
                            item_total = 0
                    total += item_total
                    try:
                        item_credit = float(item.get("credit") or 0)
                    except (TypeError, ValueError):
                        item_credit = 0.0
                    attempt_credits += item_credit
                    credits += item_credit
                    try:
                        status = int(item.get("status_code") or 0)
                    except (TypeError, ValueError):
                        status = 0
                    if status:
                        last_status = status
                    if item.get("finish_reason"):
                        last_finish = item.get("finish_reason")
                if attempt_credits or items:
                    self.store.set_attempt_credits(
                        request_id, attempt_no, round(attempt_credits, 6)
                    )

            if failed:
                bridge_failures += 1
                continue
            if not found:
                self.store.mark_reconcile_unknown(request_id)
                unknown += 1
                continue
            if not total:
                total = prompt + completion
            status = input_status if input_status is not None else (last_status or 200)
            self.store.reconcile_request(
                request_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                credits=round(credits, 6),
                status_code=int(status),
                finish_reason=last_finish,
            )
            reconciled += 1

        return {
            "checked": checked,
            "reconciled": reconciled,
            "unknown": unknown,
            "bridge_failures": bridge_failures,
        }
