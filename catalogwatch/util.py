from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def local_day(value: str | None = None) -> str:
    """Return the Asia/Shanghai calendar day for an ISO timestamp.

    Daily quotas and the console's "today" figures must follow the same
    Beijing calendar as the timestamps users see, not the UTC day boundary
    (which falls at 08:00 Beijing and splits a working day in two).
    """
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = utcnow()
    return parsed.astimezone(SHANGHAI).date().isoformat()
