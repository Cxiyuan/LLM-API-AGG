"""Day/night pricing windows and priority tiers for the pool scheduler.

WorkBuddy promotions are the source of truth for pricing windows. Some rules
carry a structured factor, while others ("错峰使用") only describe the discount
in their Chinese hover text. This module fills that gap so routing and the
console use the same precise price at the current moment, and exposes the
derived night window instead of hard-coding one.

All clocks are evaluated in the promotion's own timezone, falling back to
Asia/Shanghai, matching `promotions.py`.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, time, timedelta
from typing import Any

from .promotions import (
    DEFAULT_TIMEZONE,
    apply_promotion,
    is_promotion_active,
    minutes_in_timezone,
    normalize_now,
    parse_clock_minutes,
    select_promotion,
)

DEFAULT_NIGHT_START = "23:00"
DEFAULT_NIGHT_END = "08:00"
DEFAULT_DAY_PRIORITY = {"free": 0, "promo": 1, "normal": 99}
DEFAULT_NIGHT_PRIORITY = {
    "night_free": 0,
    "night_promo": 1,
    "free": 2,
    "promo": 3,
    "normal": 99,
}

_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*(?:[–—~～-]|至)\s*(\d{1,2}:\d{2})"
)
_WEEKDAY_RE = re.compile(r"周([一二三四五六日天])\s*(?:至|到|[-–—])\s*周([一二三四五六日天])")
_DISCOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*折")
_NIGHT_MARKERS = ("夜间", "晚上", "夜間", "night")
_FREE_MARKERS = ("免费", "限免", "免单", "免费额度", "free")
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _text_of(rule: dict) -> str:
    return " ".join(
        str(rule.get(key) or "")
        for key in ("label", "text_zh", "text_en")
    ).strip()


def _minute_clock(minutes: int) -> str:
    minutes = int(minutes) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def night_window(policy: dict | None, costs: list[dict] | None = None) -> tuple[int, int, str]:
    """Return (start_minute, end_minute, source) for the current night period."""
    policy = policy or {}
    if bool(policy.get("night_auto", True)) and costs:
        derived = derive_night_window(costs)
        if derived is not None:
            return derived[0], derived[1], "auto"
    start = parse_clock_minutes(policy.get("night_start")) if policy else None
    end = parse_clock_minutes(policy.get("night_end")) if policy else None
    if start is None or end is None or start == end:
        start = parse_clock_minutes(DEFAULT_NIGHT_START) or 1380
        end = parse_clock_minutes(DEFAULT_NIGHT_END) or 480
    return start, end, "manual"


def derive_night_window(costs: list[dict] | None) -> tuple[int, int] | None:
    """Infer the night window from rules whose label/text/window is nightly.

    The most common start/end among nightly promotions wins (both WorkBuddy
    night discounts use 23:00); ties keep the earliest start and latest end.
    """
    starts: Counter[int] = Counter()
    ends: Counter[int] = Counter()
    for item in costs or []:
        for rule in item.get("promotions") or []:
            if not isinstance(rule, dict) or rule.get("factor") is None:
                continue
            if not rule_is_night(rule):
                continue
            schedule = rule.get("schedule")
            daily = schedule.get("daily") if isinstance(schedule, dict) else None
            for window in daily if isinstance(daily, list) else []:
                if not isinstance(window, dict):
                    continue
                start = parse_clock_minutes(window.get("start"))
                end = parse_clock_minutes(window.get("end"))
                if start is None or end is None or start == end:
                    continue
                if start < end and not (start >= 21 * 60 or end <= 9 * 60):
                    continue
                starts[start] += 1
                ends[end] += 1
    if not starts or not ends:
        return None
    start = max(starts, key=lambda value: (starts[value], -value))
    end = max(ends, key=lambda value: (ends[value], value))
    return start, end


def is_night(now: datetime | None, start: int, end: int) -> bool:
    moment = normalize_now(now)
    minutes = minutes_in_timezone(moment, DEFAULT_TIMEZONE)
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def rule_is_night(rule: dict | None) -> bool:
    if not isinstance(rule, dict):
        return False
    text = _text_of(rule).lower()
    if any(marker in text for marker in _NIGHT_MARKERS):
        return True
    schedule = rule.get("schedule")
    daily = schedule.get("daily") if isinstance(schedule, dict) else None
    for window in daily if isinstance(daily, list) else []:
        if not isinstance(window, dict):
            continue
        start = parse_clock_minutes(window.get("start"))
        end = parse_clock_minutes(window.get("end"))
        if start is None or end is None or start == end:
            continue
        if start > end:
            return True
        if start >= 22 * 60 or end <= 8 * 60:
            return True
    return False


def _weekday_span(text: str) -> tuple[int, int] | None:
    match = _WEEKDAY_RE.search(text)
    if not match:
        return None
    first = _WEEKDAY_INDEX.get(match.group(1))
    last = _WEEKDAY_INDEX.get(match.group(2))
    if first is None or last is None:
        return None
    return first, last


def _weekday_in_span(weekday: int, span: tuple[int, int]) -> bool:
    first, last = span
    if first <= last:
        return first <= weekday <= last
    return weekday >= first or weekday <= last


def _text_factor(rule: dict, now: datetime | None) -> float | None:
    """Infer a discount factor from promotion hover text.

    Examples:
      * "非高峰期积分5折" with peak ranges in the text: 1.0 inside the peak
        ranges on weekdays, 0.5 outside.
      * "限时免费" / "每晚…享免费额度": 0.0.
    """
    text = _text_of(rule)
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _FREE_MARKERS):
        return 0.0
    match = _DISCOUNT_RE.search(text)
    if not match:
        return None
    try:
        factor = float(match.group(1)) / 10.0
    except ValueError:
        return None
    if factor > 1:
        factor /= 10.0
    if factor < 0 or factor >= 1:
        return None
    if "高峰" not in text:
        return factor

    moment = normalize_now(now).astimezone(_zone_of(rule))
    span = _weekday_span(text)
    if span is not None and not _weekday_in_span(moment.weekday(), span):
        return factor
    minutes = moment.hour * 60 + moment.minute
    for start_text, end_text in _RANGE_RE.findall(text):
        start = parse_clock_minutes(start_text)
        end = parse_clock_minutes(end_text)
        if start is None or end is None or start == end:
            continue
        if start < end and start <= minutes < end:
            return 1.0
        if start > end and (minutes >= start or minutes < end):
            return 1.0
    return factor


def _zone_of(rule: dict):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    schedule = rule.get("schedule")
    name = schedule.get("timezone") if isinstance(schedule, dict) else None
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        try:
            return ZoneInfo(DEFAULT_TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")


def enrich_promotions(
    promotions: list[dict] | None, now: datetime | None = None
) -> list[dict]:
    """Return copies with text-only factors filled in for the current moment."""
    enriched: list[dict] = []
    for rule in promotions or []:
        if not isinstance(rule, dict):
            continue
        copy = dict(rule)
        if copy.get("factor") is None:
            copy["factor"] = _text_factor(copy, now)
        enriched.append(copy)
    return enriched


def evaluate(
    base_credits: float | None,
    promotions: list[dict] | None,
    now: datetime | None = None,
) -> dict:
    """Apply promotions with text inference and attach the applied rule."""
    enriched = enrich_promotions(promotions, now)
    applied = apply_promotion(base_credits, enriched, now)
    rule, _active, _discounted = select_promotion(enriched, now)
    applied = dict(applied)
    applied["promotions"] = enriched
    applied["promotion_rule"] = rule
    applied["night_specific"] = rule_is_night(rule)
    applied["promotion_label"] = (rule or {}).get("label")
    return applied


def reference_costs(
    base_credits: float | None,
    promotions: list[dict] | None,
    now: datetime | None = None,
    *,
    night_start: int = 1380,
    night_end: int = 480,
) -> dict:
    """Effective price at a representative daytime and night moment."""
    moment = normalize_now(now).astimezone(_zone_of({}))
    day = moment.replace(
        hour=10, minute=0, second=0, microsecond=0, tzinfo=moment.tzinfo
    )
    night_minutes = (night_start + 30) % 1440
    night = moment.replace(
        hour=night_minutes // 60,
        minute=night_minutes % 60,
        second=0,
        microsecond=0,
        tzinfo=moment.tzinfo,
    )
    if night <= moment:
        night = night + timedelta(days=1)
    day_applied = evaluate(base_credits, promotions, day)
    night_applied = evaluate(base_credits, promotions, night)
    return {
        "day_credits": day_applied["credits"],
        "day_free": day_applied["free"],
        "night_credits": night_applied["credits"],
        "night_free": night_applied["free"],
    }


def is_full_price(item: dict) -> bool:
    """True when a model would bill at its base price (or has no usable price).

    Automatic scheduling is only allowed to use free models and models with a
    currently active discount. Explicit model requests bypass this check.
    """
    if bool(item.get("free")):
        return False
    credits = item.get("credits")
    if credits is None:
        return True
    try:
        value = float(credits)
    except (TypeError, ValueError):
        return True
    if value == 0:
        return False
    factor = item.get("promotion_factor")
    if bool(item.get("promotion_active")) and factor is not None:
        try:
            if float(factor) < 1:
                return False
        except (TypeError, ValueError):
            pass
    base = item.get("base_credits")
    try:
        if base is not None and value < float(base):
            return False
    except (TypeError, ValueError):
        pass
    return True


def tier_bucket(item: dict, period: str, *, allow_night: bool = True) -> str:
    """Classify a cost item into the editable scheduling buckets."""
    credits = item.get("credits")
    free = bool(item.get("free")) and credits == 0
    promo = bool(item.get("promotion_active")) and item.get("promotion_factor") is not None
    night_specific = bool(item.get("night_specific"))
    if period == "night" and allow_night:
        if free and night_specific:
            return "night_free"
        if free:
            return "free"
        if promo:
            return "night_promo"
        if credits is not None:
            return "normal"
        return "normal"
    if free:
        return "free"
    if promo:
        return "promo"
    return "normal"


def priority_rank(
    item: dict,
    period: str,
    policy: dict | None,
    *,
    allow_night: bool = True,
) -> int:
    policy = policy or {}
    key = "night_priority" if period == "night" and allow_night else "day_priority"
    defaults = (
        DEFAULT_NIGHT_PRIORITY if key == "night_priority" else DEFAULT_DAY_PRIORITY
    )
    priorities = dict(defaults)
    configured = policy.get(key)
    if isinstance(configured, dict):
        for bucket, rank in configured.items():
            try:
                priorities[str(bucket)] = int(rank)
            except (TypeError, ValueError):
                continue
    bucket = tier_bucket(item, period, allow_night=allow_night)
    return int(priorities.get(bucket, 99))


def period_now(
    policy: dict | None, costs: list[dict] | None, now: datetime | None = None
) -> dict:
    """Describe the active scheduling period and the source of its window."""
    start, end, source = night_window(policy, costs)
    active = is_night(now, start, end) and bool((policy or {}).get("allow_night", True))
    return {
        "period": "night" if active else "day",
        "night_window": {
            "start": _minute_clock(start),
            "end": _minute_clock(end),
            "source": source,
        },
        "night_start_minutes": start,
        "night_end_minutes": end,
    }
