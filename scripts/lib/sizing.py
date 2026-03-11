"""
Position sizing for Engine v2.

target_size = BASE_SIZE_USD × (bndpy / endpy)
Capped per-leg by: MAX_POSITION_PCT × NAV, free_margin, MAX_LEVERAGE × free_margin.
Both legs same size (use lower cap).
"""
from __future__ import annotations

import os

import asyncpg

BASE_SIZE_USD = float(os.environ.get("BASE_SIZE_USD", "200"))
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.20"))
MAX_LEVERAGE = float(os.environ.get("MAX_LEVERAGE", "5"))


async def _venue_free_margin(pool: asyncpg.Pool, venue: str) -> float:
    """Get free margin for a venue from latest_accounts view."""
    row = await pool.fetchrow(
        "SELECT free_margin FROM latest_accounts WHERE venue = $1", venue
    )
    if not row or row["free_margin"] is None:
        return 0.0
    return float(row["free_margin"])


def _leg_cap(nav: float, free_margin: float) -> float:
    """Compute the maximum position size for one leg."""
    nav_cap = MAX_POSITION_PCT * nav
    margin_cap = free_margin
    leverage_cap = MAX_LEVERAGE * free_margin
    return min(nav_cap, margin_cap, leverage_cap)


async def compute_size(
    pool: asyncpg.Pool,
    bndpy: float,
    endpy: float,
    nav: float,
    short_venue: str,
    long_venue: str,
) -> float:
    """Compute position size in USD for both legs.

    Returns the final size (same for both legs), or 0 if below BASE_SIZE_USD.
    """
    if endpy <= 0 or bndpy <= 0:
        return 0.0

    target = BASE_SIZE_USD * (bndpy / endpy)

    short_margin = await _venue_free_margin(pool, short_venue)
    long_margin = await _venue_free_margin(pool, long_venue)

    short_cap = _leg_cap(nav, short_margin)
    long_cap = _leg_cap(nav, long_margin)

    # Both legs same size — use the lower cap
    size = min(target, short_cap, long_cap)

    if size < BASE_SIZE_USD:
        return 0.0

    return round(size, 2)
