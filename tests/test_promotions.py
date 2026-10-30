from __future__ import annotations

from datetime import datetime, timezone

from catalogwatch.workbuddy.promotions import (
    apply_promotion,
    is_promotion_active,
    normalize_promotion,
    parse_clock_minutes,
    parse_credits,
)


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def schedule(daily=None, timezone_name="Asia/Shanghai", valid_until=None):
    value = {"timezone": timezone_name}
    if daily is not None:
        value["daily"] = daily
    if valid_until:
        value["validUntil"] = valid_until
    return value


def promotion(factor, daily=None, label="test", active_only=False, valid_until=None):
    return normalize_promotion(
        {
            "enabled": True,
            "badge": {"label": label, "display": "activeOnly" if active_only else None},
            "discount": {"factor": factor} if factor is not None else {},
            "schedule": schedule(daily, valid_until=valid_until),
        }
    )


def test_parse_clock_minutes():
    assert parse_clock_minutes("23:00") == 1380
    assert parse_clock_minutes("7:50") == 470
    assert parse_clock_minutes("24:00") == 1440
    assert parse_clock_minutes("24:01") is None
    assert parse_clock_minutes("7:5") is None
    assert parse_clock_minutes("abc") is None


def test_parse_credits_numeric_and_x_strings():
    assert parse_credits(0.79) == 0.79
    assert parse_credits("x0.79") == 0.79
    assert parse_credits("x0") == 0.0
    assert parse_credits("0x") is None  # promotion factor syntax, not a base credit
    assert parse_credits("0.50x") is None
    assert parse_credits(None) is None
    assert parse_credits(True) is None


def test_normalize_promotion_factor_formats():
    zero = normalize_promotion(
        {
            "enabled": True,
            "badge": {"label": "夜间免费"},
            "discount": {"discountedCredits": "0x"},
            "schedule": schedule([{"start": "23:00", "end": "08:00"}]),
        }
    )
    assert zero is not None and zero["factor"] == 0.0
    assert zero["label"] == "夜间免费"

    half = normalize_promotion(
        {
            "enabled": True,
            "badge": "夜间折扣",
            "discount": {"discountedCredits": "0.50x"},
            "schedule": schedule([]),
        }
    )
    assert half is not None and half["factor"] == 0.5

    assert normalize_promotion({"enabled": True, "discount": {}}) is None
    assert normalize_promotion({"enabled": False, "badge": "x", "discount": {"factor": 0}}) is None


def test_night_window_crosses_midnight_in_schedule_timezone():
    rule = promotion(0.5, [{"start": "23:00", "end": "08:00"}])
    assert rule is not None
    assert is_promotion_active(rule, utc(2026, 10, 25, 15, 30)) is True   # Shanghai 23:30
    assert is_promotion_active(rule, utc(2026, 10, 25, 2, 0)) is False    # Shanghai 10:00
    assert is_promotion_active(rule, utc(2026, 10, 25, 23, 59)) is True   # Shanghai 07:59
    assert is_promotion_active(rule, utc(2026, 10, 26, 0, 0)) is False    # Shanghai 08:00


def test_valid_until_is_exclusive():
    rule = promotion(0, [], label="限时", active_only=True, valid_until="2026-11-01T00:00:00+08:00")
    assert rule is not None
    assert is_promotion_active(rule, utc(2026, 10, 31, 15, 59)) is True
    assert is_promotion_active(rule, utc(2026, 10, 31, 16, 0)) is False


def test_all_day_window_and_zero_length_window():
    all_day = promotion(0, [{"start": "00:00", "end": "00:00"}])
    assert all_day is not None and is_promotion_active(all_day, utc(2026, 10, 25, 12, 0))
    zero = promotion(0.5, [{"start": "10:00", "end": "10:00"}])
    assert zero is not None and not is_promotion_active(zero, utc(2026, 10, 25, 2, 0))


def test_apply_promotion_free_low_and_unknown():
    night = promotion(0.5, [{"start": "23:00", "end": "08:00"}])
    day = apply_promotion(0.79, [night], utc(2026, 10, 25, 2, 0))
    assert day["credits"] == 0.79 and day["free"] is False

    active = apply_promotion(0.79, [night], utc(2026, 10, 25, 15, 30))
    assert active["credits"] == 0.395
    assert active["free"] is False
    assert active["promotion_active"] is True

    free = apply_promotion(0.29, [promotion(0, [])], utc(2026, 10, 25, 12, 0))
    assert free["free"] is True and free["credits"] == 0.0

    unknown_base = apply_promotion(None, [promotion(0, [])], utc(2026, 10, 25, 12, 0))
    assert unknown_base["free"] is True and unknown_base["credits"] is None
