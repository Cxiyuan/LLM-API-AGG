from __future__ import annotations

import difflib
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def html_to_text(html: str, selector: str | None = None) -> str:
    """Reduce an HTML document to normalized, line-oriented visible text.

    Scripts/styles/navigation boilerplate are removed. Consecutive duplicate
    lines are collapsed so that layout churn does not look like a content
    change.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()

    node = None
    if selector:
        node = soup.select_one(selector)
        if node is None:
            raise ValueError(f"selector {selector!r} matched nothing")

    root = node or soup.body or soup
    text = root.get_text("\n")

    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if not line:
            continue
        if not lines or lines[-1] != line:
            lines.append(line)

    return "\n".join(lines)[:200_000]


def text_diff(old: str, new: str, max_lines: int = 200) -> tuple[str, int, int]:
    """Return (unified diff, added lines, removed lines)."""
    diff_lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="before",
            tofile="after",
            n=2,
            lineterm="",
        )
    )
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    if len(diff_lines) > max_lines:
        omitted = len(diff_lines) - max_lines
        diff_lines = diff_lines[:max_lines] + [f"... diff truncated ({omitted} more lines)"]
    return "\n".join(diff_lines), added, removed


_SINCE_RE = re.compile(r"^(\d+)([mhd])$")


def parse_since(value: str) -> str:
    """Accept an ISO timestamp or a relative window such as 90m, 24h, 7d."""
    value = value.strip()
    match = _SINCE_RE.fullmatch(value)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        return (utcnow() - delta).isoformat(timespec="seconds")

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except ValueError:
        return ""
