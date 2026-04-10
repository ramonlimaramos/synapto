"""Decay scoring — calculates relevance scores based on depth layer, age, and access frequency."""

from __future__ import annotations

import math
from datetime import UTC, datetime

# Half-life in hours per depth layer — after this many hours, decay_score drops to 0.5
HALF_LIFE_HOURS = {
    "core": float("inf"),     # never decays
    "stable": 24 * 30 * 6,    # ~6 months
    "working": 24 * 7,        # 1 week
    "ephemeral": 6,           # 6 hours
}

# Access count boost — each access slightly increases the score
ACCESS_BOOST_FACTOR = 0.02
ACCESS_BOOST_CAP = 0.3


def calculate_decay_score(
    depth_layer: str,
    created_at: datetime,
    accessed_at: datetime,
    access_count: int,
    now: datetime | None = None,
) -> float:
    """Calculate a decay score between 0.0 and ~1.3.

    Formula:
        base = 2^(-hours_since_access / half_life)
        boost = min(access_count * ACCESS_BOOST_FACTOR, ACCESS_BOOST_CAP)
        score = base + boost

    Core memories always return 1.0 + boost.
    """
    if now is None:
        now = datetime.now(UTC)

    half_life = HALF_LIFE_HOURS.get(depth_layer, HALF_LIFE_HOURS["working"])

    if math.isinf(half_life):
        base = 1.0
    else:
        # use accessed_at for recency — accessing refreshes relevance
        hours_elapsed = max(0, (now - accessed_at).total_seconds() / 3600)
        base = 2 ** (-hours_elapsed / half_life)

    boost = min(access_count * ACCESS_BOOST_FACTOR, ACCESS_BOOST_CAP)
    return round(base + boost, 6)
