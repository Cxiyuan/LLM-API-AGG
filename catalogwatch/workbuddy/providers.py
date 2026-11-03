"""Bridge channel identifiers shared by the pool and the SQLite layer.

Buddy2api is the source of truth for which channels can be loaded.  These are
the channels the pool knows how to name, route, and display; a channel with no
imported account is simply skipped during catalog refresh.
"""

from __future__ import annotations

#: Buddy2api channels. Order matters for the console and fallback lists.
BUDDY_PROVIDER_ORDER: tuple[str, ...] = (
    "workbuddy",
    "workbuddy-i",
    "qoder",
    "qoder-i",
    "qwenwork",
    "traework",
)

#: Providers whose catalog can be enriched by the WorkBuddy config endpoint.
WORKBUDDY_PROVIDERS: tuple[str, ...] = ("workbuddy", "workbuddy-i")

#: Standalone OpenAI-compatible channel. It has no bridge account table.
OPENBRIDGE_PROVIDER = "openbridge"

#: All providers the unified gateway may route to.
ROUTABLE_PROVIDER_ORDER: tuple[str, ...] = BUDDY_PROVIDER_ORDER + (OPENBRIDGE_PROVIDER,)

#: Channels billed by their own quota rather than WorkBuddy credits.
QUOTA_PROVIDERS: frozenset[str] = frozenset(
    provider
    for provider in ROUTABLE_PROVIDER_ORDER
    if provider not in WORKBUDDY_PROVIDERS
)

#: Providers without a Buddy2api account pool.
ACCOUNTLESS_PROVIDERS: frozenset[str] = frozenset({OPENBRIDGE_PROVIDER})

BRIDGE_PROVIDERS: frozenset[str] = frozenset(ROUTABLE_PROVIDER_ORDER)
