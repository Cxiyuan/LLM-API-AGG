"""WorkBuddy promotion/cost semantics.

This is a dependency-free Python port of the reference bridge's
`bridge/promotions.mjs`. It intentionally keeps the same edge semantics:

- validFrom inclusive, validUntil exclusive
- daily windows are evaluated in the schedule timezone
- start > end is a cross-midnight window (e.g. 23:00–08:00)
- 00:00–00:00 means all day; other start == end windows are ignored
- the lowest active discount wins; badge-only rules never change cost
- an unknown base multiplier stays unknown (never coerced to 0)
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_FACTOR_RE = re.compile(r"^([0-9]*\.?[0-9]+)x$", re.IGNORECASE)
_COST_RE = re.compile(r"x\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        try:
            return ZoneInfo(DEFAULT_TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")


def _as_datetime(value: Any, tz_name: str | None = None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(tz_name))
    return parsed


def normalize_now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def parse_clock_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = _CLOCK_RE.fullmatch(value.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if minute > 59 or hour > 24:
        return None
    total = hour * 60 + minute
    return total if total <= 1440 else None


def minutes_in_timezone(moment: datetime, tz_name: str | None) -> int:
    local = normalize_now(moment).astimezone(_zone(tz_name))
    return local.hour * 60 + local.minute


def promotion_factor(discount: Any) -> float | None:
    if not isinstance(discount, dict):
        return None
    factor = discount.get("factor")
    if isinstance(factor, bool):
        return None
    if isinstance(factor, (int, float)) and math.isfinite(float(factor)) and float(factor) >= 0:
        return float(factor)
    raw = discount.get("discountedCredits")
    if isinstance(raw, str):
        match = _FACTOR_RE.fullmatch(raw.strip())
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def parse_credits(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
            return number if math.isfinite(number) else None
        except ValueError:
            pass
        match = _COST_RE.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def normalize_promotion(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or raw.get("enabled") is False:
        return None

    label = ""
    color = None
    active_only = False
    badge = raw.get("badge")
    if isinstance(badge, dict):
        for key in ("label", "text", "name", "value"):
            if isinstance(badge.get(key), str) and badge[key].strip():
                label = badge[key].strip()
                break
        if isinstance(badge.get("color"), str):
            color = badge["color"]
        active_only = badge.get("display") == "activeOnly"
    elif isinstance(badge, str):
        label = badge.strip()

    factor = promotion_factor(raw.get("discount"))
    if not label and factor is None:
        return None

    hover = raw.get("hover")
    text_zh = ""
    text_en = ""
    if isinstance(hover, str):
        text_zh = hover
    elif isinstance(hover, dict):
        if isinstance(hover.get("textZh"), str):
            text_zh = hover["textZh"]
        if isinstance(hover.get("textEn"), str):
            text_en = hover["textEn"]

    schedule = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else None
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0

    return {
        "label": label or None,
        "color": color,
        "active_only": bool(active_only),
        "factor": factor,
        "priority": priority,
        "schedule": schedule,
        "text_zh": text_zh,
        "text_en": text_en,
    }


def group_promotions(raw_promotions: Any) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    if not isinstance(raw_promotions, list):
        return grouped
    for raw in raw_promotions:
        rule = normalize_promotion(raw)
        if rule is None or not isinstance(raw, dict):
            continue
        model_ids = raw.get("modelIds")
        if not isinstance(model_ids, list):
            continue
        for model_id in model_ids:
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            grouped.setdefault(model_id.strip(), []).append(rule)
    return grouped


def is_promotion_active(promotion: dict, now: datetime | None = None) -> bool:
    if not isinstance(promotion, dict):
        return False
    schedule = promotion.get("schedule")
    if not isinstance(schedule, dict):
        return True
    tz_name = schedule.get("timezone") if isinstance(schedule.get("timezone"), str) else None
    moment = normalize_now(now)

    valid_from = _as_datetime(schedule.get("validFrom"), tz_name)
    valid_until = _as_datetime(schedule.get("validUntil"), tz_name)
    if valid_from is not None and moment < valid_from:
        return False
    if valid_until is not None and moment >= valid_until:
        return False

    daily = schedule.get("daily")
    if not isinstance(daily, list) or not daily:
        return True
    minutes = minutes_in_timezone(moment, tz_name)
    for window in daily:
        if not isinstance(window, dict):
            continue
        start = parse_clock_minutes(window.get("start"))
        end = parse_clock_minutes(window.get("end"))
        if start is None or end is None:
            continue
        if start == end:
            if start == 0:
                return True
            continue
        if start < end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:
            return True
    return False


def select_promotion(
    promotions: list[dict] | None, now: datetime | None = None
) -> tuple[dict | None, bool, bool]:
    rules = [rule for rule in (promotions or []) if isinstance(rule, dict)]
    active = [rule for rule in rules if is_promotion_active(rule, now)]
    discounts = [
        rule
        for rule in active
        if rule.get("factor") is not None and float(rule["factor"]) < 1.0
    ]
    discounts.sort(
        key=lambda rule: (float(rule["factor"]), -int(rule.get("priority") or 0))
    )
    if discounts:
        return discounts[0], True, True

    scheduled = sorted(active, key=lambda rule: -int(rule.get("priority") or 0))
    if scheduled:
        return scheduled[0], True, False

    marketing = [
        rule
        for rule in rules
        if rule.get("label") and not rule.get("active_only")
    ]
    marketing.sort(key=lambda rule: -int(rule.get("priority") or 0))
    if marketing:
        return marketing[0], False, False
    return None, False, False


def apply_promotion(
    base_credits: Any,
    promotions: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    base = parse_credits(base_credits)
    rule, active, discounted = select_promotion(promotions, now)

    credits = base
    factor = rule.get("factor") if rule else None
    if discounted and factor is not None and base is not None:
        credits = round(base * float(factor), 3)
    free = bool(discounted and factor == 0) or (credits is not None and credits == 0)

    schedule = (rule or {}).get("schedule") if isinstance(rule, dict) else None
    promotion_until = ""
    if isinstance(schedule, dict) and isinstance(schedule.get("validUntil"), str):
        promotion_until = schedule["validUntil"]

    return {
        "credits": credits,
        "free": free,
        "badge": (rule or {}).get("label") if rule else None,
        "badge_color": (rule or {}).get("color") if rule else None,
        "promotion_active": bool(discounted),
        "promotion_factor": float(factor) if discounted and factor is not None else None,
        "promotion_until": promotion_until,
        "promotion_text_zh": (rule or {}).get("text_zh") if rule else "",
        "promotion_text_en": (rule or {}).get("text_en") if rule else "",
    }


def manual_rule(
    *,
    label: str = "manual",
    factor: float | None = None,
    base_credits: float | None = None,
    daily_windows: list[dict] | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    valid_from: str | None = None,
    valid_until: str | None = None,
) -> dict:
    schedule: dict[str, Any] = {"timezone": timezone_name}
    if daily_windows:
        schedule["daily"] = daily_windows
    if valid_from:
        schedule["validFrom"] = valid_from
    if valid_until:
        schedule["validUntil"] = valid_until
    return {
        "label": label,
        "color": None,
        "active_only": False,
        "factor": factor,
        "priority": 1000,
        "schedule": schedule,
        "text_zh": "",
        "text_en": "",
        "base_credits": base_credits,
    }
