from __future__ import annotations

import httpx

from catalogwatch.config import (
    ExtractorConfig,
    NotifyConfig,
    ProviderConfig,
    QuotaSourceConfig,
    Settings,
)
from catalogwatch.db import Database
from catalogwatch.extract import LLMExtractor
from catalogwatch.quota import QuotaMonitor


async def test_quota_change_detection(tmp_path):
    page = {"html": "<html><body><h1>Free tier</h1><p>30 RPM</p></body></html>"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page["html"], headers={"content-type": "text/html"})

    provider = ProviderConfig(
        id="p",
        name="Provider",
        adapter="openai_models",
        base_url="https://example.com/v1",
        quota_sources=[
            QuotaSourceConfig(label="limits", url="https://example.com/limits", interval_minutes=60)
        ],
    )
    settings = Settings(
        providers=[provider],
        notify=NotifyConfig(macos=False),
        extractor=ExtractorConfig(),
        db_path=tmp_path / "quota.db",
        config_path=tmp_path / "providers.yaml",
    )
    db = Database(settings.db_path)
    db.sync_providers([provider])
    db.sync_quota_sources([provider])
    monitor = QuotaMonitor(
        settings,
        db,
        LLMExtractor(settings.extractor),
        transport=httpx.MockTransport(handler),
    )

    source = db.quota_sources()[0]
    assert await monitor.check(source, provider) == []  # baseline
    assert db.quota_source("p:limits")["last_hash"] is not None

    source = db.quota_source("p:limits")
    assert await monitor.check(source, provider) == []  # unchanged

    page["html"] = "<html><body><h1>Free tier</h1><p>60 RPM</p><p>1,000 RPD</p></body></html>"
    source = db.quota_source("p:limits")
    events = await monitor.check(source, provider)

    assert len(events) == 1
    assert events[0].kind == "quota.changed"
    assert "+60 RPM" in events[0].payload["diff"]
    updated = db.quota_source("p:limits")
    assert updated["last_changed_at"] is not None
    assert "60 RPM" in (db.latest_quota_text("p:limits") or "")
    db.close()


async def test_fields_extraction_from_client_rendered_page(tmp_path):
    page = {
        "html": (
            '<html><body><div id="app"></div><script>window.__DATA__='
            '{"rateLimits":{"requestsPerMinute":"Up to 40 rpm",'
            '"requestsPerDay":"10,000 requests per day"}};</script></body></html>'
        )
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page["html"], headers={"content-type": "text/html"})

    provider = ProviderConfig(
        id="p",
        name="Provider",
        adapter="openai_models",
        base_url="https://example.com/v1",
        quota_sources=[
            QuotaSourceConfig(
                label="rate-limits",
                url="https://example.com/faq",
                interval_minutes=60,
                fields=["requestsPerMinute", "requestsPerDay"],
            )
        ],
    )
    settings = Settings(
        providers=[provider],
        notify=NotifyConfig(macos=False),
        extractor=ExtractorConfig(),
        db_path=tmp_path / "quota.db",
        config_path=tmp_path / "providers.yaml",
    )
    db = Database(settings.db_path)
    db.sync_providers([provider])
    db.sync_quota_sources([provider])
    monitor = QuotaMonitor(
        settings,
        db,
        LLMExtractor(settings.extractor),
        transport=httpx.MockTransport(handler),
    )

    source = db.quota_sources()[0]
    assert source["fields"] == ["requestsPerMinute", "requestsPerDay"]
    assert await monitor.check(source, provider) == []  # baseline

    baseline = db.latest_quota_text("p:rate-limits") or ""
    assert "requestsPerMinute: Up to 40 rpm" in baseline
    assert "requestsPerDay: 10,000 requests per day" in baseline

    page["html"] = page["html"].replace("Up to 40 rpm", "Up to 20 rpm")
    source = db.quota_source("p:rate-limits")
    events = await monitor.check(source, provider)
    assert len(events) == 1
    assert "-requestsPerMinute: Up to 40 rpm" in events[0].payload["diff"]
    assert "+requestsPerMinute: Up to 20 rpm" in events[0].payload["diff"]
    db.close()
