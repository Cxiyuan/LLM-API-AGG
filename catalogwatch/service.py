from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import Database
from .extract import LLMExtractor
from .keystore import ProviderKeyStore
from .models import Event
from .notify import Notifier
from .poller import ModelPoller, PollResult
from .quota import QuotaMonitor

log = logging.getLogger(__name__)


class CatalogService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.sync_providers(settings.providers)
        self.db.sync_quota_sources(settings.providers)

        self.provider_map = {provider.id: provider for provider in settings.providers}
        self.key_store = ProviderKeyStore(self.db)
        self.notifier = Notifier(settings.notify)
        self.extractor = LLMExtractor(settings.extractor)
        self.poller = ModelPoller(settings, self.db, self.key_store.resolve)
        self.quota = QuotaMonitor(settings, self.db, self.extractor)
        self._locks: dict[str, asyncio.Lock] = {}

    def close(self) -> None:
        self.db.close()

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def poll_provider(
        self, provider_id: str, notify: bool = True
    ) -> tuple[PollResult, list[Event]]:
        cfg = self.provider_map.get(provider_id)
        if cfg is None:
            raise KeyError(f"unknown provider: {provider_id}")
        async with self._lock(f"poll:{provider_id}"):
            result, events = await self.poller.poll(cfg)
        if notify:
            await self.notifier.send_events(events)
        return result, events

    async def poll_all(
        self, force: bool = True, notify: bool = True
    ) -> tuple[list[PollResult], list[Event]]:
        results: list[PollResult] = []
        all_events: list[Event] = []

        for cfg in self.settings.providers:
            if not cfg.enabled:
                continue
            if not force and not self.db.provider_due(cfg):
                continue
            async with self._lock(f"poll:{cfg.id}"):
                result, events = await self.poller.poll(cfg)
            results.append(result)
            all_events.extend(events)

        if notify:
            await self.notifier.send_events(all_events)
        return results, all_events

    async def check_quotas(self, force: bool = True, notify: bool = True) -> list[Event]:
        events: list[Event] = []
        for source in self.db.quota_sources():
            provider = self.provider_map.get(source["provider_id"])
            if provider is None or not provider.enabled:
                continue
            if not force and not self.db.quota_source_due(source):
                continue
            async with self._lock(f"quota:{source['id']}"):
                events.extend(await self.quota.check(source, provider))

        if notify:
            await self.notifier.send_events(events)
        return events

    async def due_work(self) -> list[PollResult]:
        results, events = await self.poll_all(force=False, notify=False)
        events.extend(await self.check_quotas(force=False, notify=False))
        if events:
            await self.notifier.send_events(events)
        return results

    async def run_forever(self, stop: asyncio.Event, interval_seconds: int = 20) -> None:
        log.info("scheduler started (tick=%ss)", interval_seconds)
        while not stop.is_set():
            try:
                await self.due_work()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("scheduler cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
        log.info("scheduler stopped")
