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
