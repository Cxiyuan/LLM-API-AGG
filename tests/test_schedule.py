from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from catalogwatch.workbuddy.schedule import (
    derive_night_window,
    is_full_price,
    evaluate,
    is_night,
    night_window,
    period_now,
    tier_bucket,
)

TZ = ZoneInfo("Asia/Shanghai")


def _offpeak_rule() -> dict:
    return {
        "label": "错峰使用",
        "color": None,
        "factor": None,
        "priority": 100,
        "active_only": True,
        "schedule": {
            "timezone": "Asia/Shanghai",
            "daily": [{"start": "00:00", "end": "23:59"}],
        },
        "text_zh": "周一至周五 09:00–12:00、14:00–18:00 属高峰原价，非高峰期积分5折",
        "text_en": "",
    }


def test_text_only_offpeak_discount_applies_to_the_right_hours():
    promotions = [_offpeak_rule()]

    peak = evaluate(0.17, promotions, datetime(2026, 11, 2, 10, 0, tzinfo=TZ))
    assert peak["credits"] == 0.17
    assert peak["promotion_active"] is False

    lunch = evaluate(0.17, promotions, datetime(2026, 11, 2, 13, 0, tzinfo=TZ))
    assert lunch["credits"] == 0.085
    assert lunch["promotion_active"] is True
    assert lunch["promotion_factor"] == 0.5

    night = evaluate(0.17, promotions, datetime(2026, 11, 2, 23, 30, tzinfo=TZ))
    assert night["credits"] == 0.085

    weekend = evaluate(0.17, promotions, datetime(2026, 11, 7, 10, 0, tzinfo=TZ))
    assert weekend["credits"] == 0.085


def test_night_window_is_derived_from_promotions():
    costs = [
        {
            "promotions": [
                {
                    "label": "夜间折扣",
                    "factor": 0.5,
                    "schedule": {
                        "timezone": "Asia/Shanghai",
                        "daily": [{"start": "23:00", "end": "7:50"}],
                    },
                    "text_zh": "",
                }
            ]
        },
        {
            "promotions": [
                {
                    "label": "夜间免费",
                    "factor": 0.0,
                    "schedule": {
                        "timezone": "Asia/Shanghai",
                        "daily": [{"start": "23:00", "end": "08:00"}],
                    },
                    "text_zh": "",
                }
            ]
        },
    ]
    assert derive_night_window(costs) == (23 * 60, 8 * 60)

    start, end, source = night_window({"night_auto": True}, costs)
    assert (start, end, source) == (23 * 60, 8 * 60, "auto")

    start, end, source = night_window(
        {"night_auto": False, "night_start": "22:30", "night_end": "07:00"}, costs
    )
    assert (start, end, source) == (22 * 60 + 30, 7 * 60, "manual")


def test_period_and_tier_buckets():
    policy = {"night_auto": False, "night_start": "23:00", "night_end": "08:00"}
    info = period_now(policy, [], datetime(2026, 11, 2, 23, 30, tzinfo=TZ))
    assert info["period"] == "night"
    assert info["night_window"] == {"start": "23:00", "end": "08:00", "source": "manual"}

    night_free = {"credits": 0.0, "free": True, "promotion_active": True, "promotion_factor": 0.0, "night_specific": True}
    night_promo = {"credits": 0.1, "free": False, "promotion_active": True, "promotion_factor": 0.5, "night_specific": True}
    general_free = {"credits": 0.0, "free": True, "promotion_active": False, "promotion_factor": None, "night_specific": False}
    free_promo = {"credits": 0.0, "free": True, "promotion_active": True, "promotion_factor": 0.0, "night_specific": False}
    normal = {"credits": 0.1, "free": False, "promotion_active": False, "promotion_factor": None, "night_specific": False}

    assert tier_bucket(night_free, "night") == "night_free"
    assert tier_bucket(night_promo, "night") == "night_promo"
    assert tier_bucket(general_free, "night") == "free"
    assert tier_bucket(free_promo, "night") == "free"
    assert tier_bucket(normal, "night") == "normal"
    assert tier_bucket(general_free, "day") == "free"
    assert tier_bucket(night_promo, "day") == "promo"

    assert is_night(datetime(2026, 11, 2, 7, 0, tzinfo=TZ), 1380, 480) is True
    assert is_night(datetime(2026, 11, 2, 12, 0, tzinfo=TZ), 1380, 480) is False


def test_full_price_detection():
    assert is_full_price({"credits": 0.2, "base_credits": 0.2, "free": False}) is True
    assert is_full_price({"credits": None, "base_credits": 0.2, "free": False}) is True
    assert is_full_price({"credits": 0.0, "base_credits": 0.2, "free": True}) is False
    assert is_full_price(
        {
            "credits": 0.1,
            "base_credits": 0.2,
            "free": False,
            "promotion_active": True,
            "promotion_factor": 0.5,
        }
    ) is False
    assert is_full_price({"credits": 0.1, "base_credits": 0.2, "free": False}) is False
