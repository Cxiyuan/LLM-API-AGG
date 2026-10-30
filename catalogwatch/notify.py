from __future__ import annotations

import asyncio
import json
import logging
import sys

import httpx

from .config import NotifyConfig, resolve_env
from .models import Event

log = logging.getLogger(__name__)

_IGNORED_KINDS = {"provider.baseline"}


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    async def send_events(self, events: list[Event]) -> None:
        events = [event for event in events if event.kind not in _IGNORED_KINDS]
        if not events:
            return

        shown = events[: self.cfg.max_events_per_message]
        lines = [event.summary for event in shown]
        if len(events) > len(shown):
            lines.append(f"... and {len(events) - len(shown)} more")

        title = f"Free LLM catalog: {len(events)} change(s)"
        body = " | ".join(lines)
        if len(body) > 700:
            body = body[:697] + "..."

        if self.cfg.macos and sys.platform == "darwin":
            await self._macos(title, body)
        await self._webhook(title, lines, events)

    async def _macos(self, title: str, body: str) -> None:
        def escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"')

        script = (
            f'display notification "{escape(body[:400])}" '
            f'with title "{escape(title[:100])}"'
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            log.debug("macOS notification failed: %s", exc)

    async def _webhook(self, title: str, lines: list[str], events: list[Event]) -> None:
        url = resolve_env(self.cfg.webhook_url_env)
        if not url:
            return

        payload = {
            "title": title,
            "text": " | ".join(lines),
            "events": [
                {
                    "kind": event.kind,
                    "provider_id": event.provider_id,
                    "subject": event.subject,
                    "summary": event.summary,
                }
                for event in events
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload)
            if response.status_code >= 400:
                log.warning("webhook returned HTTP %s", response.status_code)
        except httpx.HTTPError as exc:
            log.warning("webhook failed: %s", exc)


def encode_event(event: Event) -> str:
    return json.dumps(event.as_dict(), sort_keys=True, default=str)
