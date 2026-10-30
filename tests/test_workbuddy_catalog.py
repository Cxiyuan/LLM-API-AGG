from __future__ import annotations

from datetime import datetime, timezone

from catalogwatch.db import Database
from catalogwatch.workbuddy.catalog import WorkBuddyCatalog
from catalogwatch.workbuddy.store import WorkBuddyStore


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_effective_cost_day_night_and_manual_override(tmp_path):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    store.upsert_models(
        "workbuddy",
        [{"id": "glm-5.2", "name": "GLM", "credits": 0.79}],
        source_identity="current",
    )
    store.replace_promotions(
        "workbuddy",
        {
            "glm-5.2": [
                {
                    "label": "夜间折扣",
                    "color": None,
                    "factor": 0.5,
                    "priority": 0,
                    "active_only": False,
                    "schedule": {
                        "daily": [{"start": "23:00", "end": "08:00"}],
                        "timezone": "Asia/Shanghai",
                    },
                    "text_zh": "",
                    "text_en": "",
                }
            ]
        },
    )
    catalog = WorkBuddyCatalog(store, None)

    day = catalog.effective_cost("workbuddy", "glm-5.2", now=utc(2026, 10, 25, 2, 0))
    assert day["credits"] == 0.79 and day["free"] is False
    assert day["cost_source"] == "upstream"

    night = catalog.effective_cost("workbuddy", "glm-5.2", now=utc(2026, 10, 25, 15, 30))
    assert night["credits"] == 0.395
    assert night["promotion_active"] is True

    store.upsert_manual_cost(
        "workbuddy",
        "glm-5.2",
        base_credits=1.0,
        factor=0,
        daily_windows=[{"start": "00:00", "end": "23:59"}],
        timezone_name="Asia/Shanghai",
        valid_from=None,
        valid_until=None,
    )
    manual = catalog.effective_cost("workbuddy", "glm-5.2", now=utc(2026, 10, 25, 2, 0))
    assert manual["base_credits"] == 1.0
    assert manual["free"] is True
    assert manual["cost_source"] == "manual"

    costs = catalog.list_costs("workbuddy")
    assert len(costs) == 1 and costs[0]["model_id"] == "glm-5.2"
    db.close()


class _FakeCatalogBridge:
    """Just enough of BuddyBridge for one catalog refresh."""

    def __init__(self, entries: list[dict]):
        self.entries = entries

    async def refresh_models(self) -> dict:
        return {}

    async def models(self) -> dict:
        return {"data": self.entries}

    async def workbuddy_config(
        self, provider: str = "workbuddy", identity: str = "current", force: bool = False
    ) -> dict:
        return {
            "models": [],
            "promotions_available": False,
            "identity_used": identity,
            "account_id": None,
        }


async def test_refresh_filters_pseudo_and_media_models(tmp_path):
    """目录刷新后：auto/default、视频/音频/图像模型不入库、不出现、不路由。"""
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    # 模拟旧版本已经存过一条媒体模型：刷新后必须变 present=0 并立刻从视图消失
    store.upsert_models(
        "workbuddy",
        [{"id": "kling-video-v3", "name": "Kling-Video-V3"}],
    )
    bridge = _FakeCatalogBridge([
        {"id": "auto", "channel": "workbuddy", "name": "auto"},
        {"id": "default", "channel": "workbuddy", "name": "Default"},
        {"id": "deepseek-v4-flash", "channel": "workbuddy", "name": "Deepseek-V4-Flash", "credits": 0.17},
        {"id": "glm-4.6v", "channel": "workbuddy", "name": "GLM-4.6V", "credits": 0.11},
        {"id": "kling-video-v3", "channel": "workbuddy", "name": "Kling-Video-V3"},
        {"id": "minimax-video-h3", "channel": "workbuddy", "name": "MiniMax-H3"},
        {"id": "hunyuan-image-alpha-edit", "channel": "workbuddy", "name": "Hunyuan Image Alpha Edit"},
    ])
    catalog = WorkBuddyCatalog(store, bridge, providers=("workbuddy",))
    result = await catalog.refresh()

    kept = [item["model_id"] for item in store.list_models("workbuddy")]
    assert kept == ["deepseek-v4-flash", "glm-4.6v"]
    assert [item["model_id"] for item in catalog.list_costs()] == kept
    assert result[0]["models"] == 2
    assert result[0]["filtered"] == 5
    # 旧行即使还在库里，也不能再被查询/绑定
    assert catalog.effective_cost("workbuddy", "kling-video-v3") is None
    db.close()
