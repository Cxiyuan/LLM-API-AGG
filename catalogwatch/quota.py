from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .config import ProviderConfig, Settings
from .db import Database
from .extract import LLMExtractor
from .models import Event
from .util import html_to_text, sha256_text, text_diff, utcnow_iso

log = logging.getLogger(__name__)


def _extract_fields(html: str, fields: list[str]) -> str:
    """Pull named values out of a JS-rendered page's embedded JSON payload.

    Some provider pages (build.nvidia.com) render client-side and embed their
    copy as escaped JSON inside a script tag, so normalized visible text is
    empty. Looking up only the named fields keeps Next.js build hashes and
    unrelated page churn out of the diff.
    """
    lines: list[str] = []
    missing: list[str] = []
    for field in fields:
        pattern = re.compile(re.escape(field) + r'\\*"?\s*:\s*\\*"([^"\\]+)')
        matches = pattern.findall(html)
        if matches:
            lines.append(f"{field}: {matches[-1].strip()}")
        else:
            missing.append(field)
    if not lines:
        raise ValueError(f"none of the configured fields were found: {', '.join(fields)}")
    if missing:
        lines.append(f"missing: {', '.join(missing)}")
    return "\n".join(lines)


class QuotaMonitor:
    """Detects changes on provider quota/pricing documentation pages.

    Providers do not expose remaining free quota through an API, so the
    authoritative page is the source of truth. We normalize the page to text,
    hash it, and diff consecutive versions.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        extractor: LLMExtractor,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.db = db
        self.extractor = extractor
        self.transport = transport

    async def check(self, source: dict[str, Any], provider: ProviderConfig) -> list[Event]:
        source_id = source["id"]
        url = source["url"]

        timeout = httpx.Timeout(45.0, connect=10.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": self.settings.user_agent},
                transport=self.transport,
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            self.db.mark_quota_checked(source_id, error=f"request failed: {exc}")
            return []

        if response.status_code >= 400:
            self.db.mark_quota_checked(source_id, error=f"HTTP {response.status_code}")
            return []

        fields = source.get("fields") or []
        try:
            if fields:
                text = _extract_fields(response.text, fields)
            else:
                text = html_to_text(response.text, source.get("selector"))
        except ValueError as exc:
            self.db.mark_quota_checked(source_id, error=str(exc))
            return []

        if not text.strip():
            self.db.mark_quota_checked(source_id, error="page has no extractable text")
            return []

        content_hash = sha256_text(text)
        now = utcnow_iso()
        previous_hash = source.get("last_hash")

        if not previous_hash:
            self.db.record_quota_doc(source_id, text, content_hash, now)
            self.db.mark_quota_checked(source_id, content_hash)
            log.info("quota baseline stored for %s (%d chars)", source_id, len(text))
            return []

        if content_hash == previous_hash:
            self.db.mark_quota_checked(source_id, content_hash)
            return []

        previous_text = self.db.latest_quota_text(source_id) or ""
        diff, added, removed = text_diff(previous_text, text)

        self.db.record_quota_doc(source_id, text, content_hash, now)
        self.db.mark_quota_checked(source_id, content_hash, changed=True)

        changes = [f"+{added}/-{removed} lines"]
        if added == 0 and removed == 0:
            changes.append("whitespace/ordering only")
        if len(text) < 500:
            changes.append("low text content; page may be JS-rendered")

        events = [
            Event(
                kind="quota.changed",
                provider_id=provider.id,
                subject=source_id,
                summary=f"{provider.name}: quota docs changed ({source['label']}, {', '.join(changes)})",
                payload={
                    "source_id": source_id,
                    "label": source["label"],
                    "url": url,
                    "added_lines": added,
                    "removed_lines": removed,
                    "chars": len(text),
                    "diff": diff,
                },
            )
        ]

        if self.extractor.enabled:
            extracted = await self.extractor.extract(provider.name, url, text)
            if extracted is not None:
                self.db.record_quota_snapshot(source_id, extracted, self.extractor.name)
                plans = extracted.get("plans")
                plan_count = len(plans) if isinstance(plans, list) else 0
                events.append(
                    Event(
                        kind="quota.extracted",
                        provider_id=provider.id,
                        subject=source_id,
                        summary=(
                            f"{provider.name}: quota extracted from {source['label']} "
                            f"({plan_count} plan(s))"
                        ),
                        payload={"source_id": source_id, "extracted": extracted},
                    )
                )

        return events
