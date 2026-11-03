"""Bridge channel identifiers shared by the pool and the SQLite layer.

Buddy2api is the source of truth for which channels can be loaded.  These are
the channels the pool knows how to name, route, and display; a channel with no
imported account is simply skipped during catalog refresh.
"""

from __future__ import annotations

#: Order matters for the console and for stable fallback lists.
BRIDGE_PROVIDER_ORDER: tuple[str, ...] = (
    "workbuddy",
    "workbuddy-i",
    "qoder",
    "qoder-i",
    "qwenwork",
    "traework",
)

#: Providers whose catalog can be enriched by the WorkBuddy config endpoint.
WORKBUDDY_PROVIDERS: tuple[str, ...] = ("workbuddy", "workbuddy-i")

#: Channels billed by their own quota rather than WorkBuddy credits.
QUOTA_PROVIDERS: frozenset[str] = frozenset(
    provider for provider in BRIDGE_PROVIDER_ORDER if provider not in WORKBUDDY_PROVIDERS
)

BRIDGE_PROVIDERS: frozenset[str] = frozenset(BRIDGE_PROVIDER_ORDER)
