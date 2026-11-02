"""Reconcile pool requests with Buddy2api's request logs.

The gateway records usage from the upstream response when available. A client
may close a streaming response as soon as it has the tool call it needs, which
leaves the pool row at 499 even though the upstream request completed normally.
The bridge log is the authoritative record for those rows: it has the final
status, model, account, tokens and credits. Reconcile every ended request from
it so the ledger reflects what WorkBuddy actually charged.
"""

from __future__ import annotations

import logging

from .bridge import BridgeError, BuddyBridge
from .store import WorkBuddyStore

log = logging.getLogger(__name__)


def _split_model(value: object) -> tuple[str | None, str | None]:
    """Split a bridge wire model (``workbuddy/hy3``) into provider and id."""
    text = str(value or "").strip()
    if not text:
        return None, None
    if "/" in text:
        head, tail = text.split("/", 1)
        return (head or None), (tail or None)
    return None, text


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
            log_status = log_model = log_provider = log_account_id = log_finish = None
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
                attempt_status = attempt_finish = None
                attempt_model = attempt_provider = None
                attempt_account_id = None
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
                    # Items are newest first; the first item is the final
                    # attempt status/model/account for this request id.
                    if attempt_status is None and status:
                        attempt_status = status
                    model_provider, model_id = _split_model(item.get("model"))
                    if attempt_model is None and model_id:
                        attempt_model = model_id
                        attempt_provider = model_provider
                    if attempt_account_id is None and item.get("account_id") is not None:
                        try:
                            attempt_account_id = int(item["account_id"])
                        except (TypeError, ValueError):
                            attempt_account_id = None
                    if attempt_finish is None and item.get("finish_reason"):
                        attempt_finish = str(item["finish_reason"])
                    if status:
                        last_status = status
                    if item.get("finish_reason"):
                        last_finish = item["finish_reason"]
                if attempt_status is not None or attempt_model is not None:
                    self.store.reconcile_attempt(
                        request_id,
                        attempt_no,
                        credits=round(attempt_credits, 6),
                        status_code=attempt_status,
                        model_id=attempt_model,
                        provider=attempt_provider,
                        account_id=attempt_account_id,
                    )
                if attempt_status is not None:
                    log_status = attempt_status
                if attempt_model is not None:
                    log_model = attempt_model
                    log_provider = attempt_provider
                if attempt_account_id is not None:
                    log_account_id = attempt_account_id
                if attempt_finish is not None:
                    log_finish = attempt_finish

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
            if log_status is not None and input_status in (None, 0, 499):
                # The client closes after the tool call it needs, so the pool
                # records 499 while the bridge log shows 200/stop. Trust the
                # bridge: it is the completed upstream request.
                status = log_status
            error_code = row.get("error_code")
            if int(status) == 200:
                error_code = None
            self.store.reconcile_request(
                request_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                credits=round(credits, 6),
                status_code=int(status),
                finish_reason=log_finish or last_finish,
                error_code=error_code,
                model_routed=log_model,
                provider=log_provider,
                bridge_account_id=log_account_id,
            )
            reconciled += 1

        return {
            "checked": checked,
            "reconciled": reconciled,
            "unknown": unknown,
            "bridge_failures": bridge_failures,
        }
