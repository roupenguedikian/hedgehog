"""
EMA computation and data quality gates for Engine v2.

Computes 15-sample EMA on predicted funding rates from TimescaleDB,
applies freshness gate (exclude stale venues) and outlier gate
(exclude rate spikes > 3σ from EMA).
"""
from __future__ import annotations

import os
import statistics
from datetime import datetime, timezone

import asyncpg

STALE_THRESHOLD_SEC = int(os.environ.get("STALE_THRESHOLD_SEC", "300"))
EMA_SPAN = int(os.environ.get("EMA_SPAN", "15"))
OUTLIER_Z_SCORE = float(os.environ.get("OUTLIER_Z_SCORE", "3.0"))

# All 6 venues (lowercase, matching DB)
VENUES = ["hyperliquid", "aster", "lighter", "apex", "dydx", "drift"]


def compute_ema(rates: list[float], span: int = EMA_SPAN) -> float:
    """Compute EMA over ordered samples (oldest → newest)."""
    alpha = 2 / (span + 1)
    ema = rates[0]
    for rate in rates[1:]:
        ema = alpha * rate + (1 - alpha) * ema
    return ema


async def is_fresh(pool: asyncpg.Pool, venue: str, symbol: str,
                   threshold_sec: int = STALE_THRESHOLD_SEC) -> bool:
    """Check if venue/symbol has data within threshold_sec."""
    row = await pool.fetchrow(
        "SELECT MAX(timestamp) AS latest FROM funding_rates "
        "WHERE venue = $1 AND symbol = $2",
        venue, symbol,
    )
    if not row or row["latest"] is None:
        return False
    age = (datetime.now(timezone.utc) - row["latest"]).total_seconds()
    return age <= threshold_sec


def is_outlier(current_rate: float, ema: float, recent_rates: list[float],
               z_threshold: float = OUTLIER_Z_SCORE) -> bool:
    """Return True if current_rate is > z_threshold σ from EMA."""
    if len(recent_rates) < 5:
        return False
    std = statistics.stdev(recent_rates)
    if std == 0:
        return False
    z_score = abs(current_rate - ema) / std
    return z_score > z_threshold


async def get_venue_emas(
    pool: asyncpg.Pool,
    symbols: list[str],
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], bool]]:
    """Compute EMA for every venue/symbol pair, applying freshness + outlier gates.

    Returns:
        ema_data:      {(venue, symbol): ema_value} for fresh venues only
        outlier_flags: {(venue, symbol): True} for outlier data points
    """
    ema_data: dict[tuple[str, str], float] = {}
    outlier_flags: dict[tuple[str, str], bool] = {}

    for venue in VENUES:
        for symbol in symbols:
            # Freshness gate
            if not await is_fresh(pool, venue, symbol):
                continue

            # Pull last 20 min of predicted_rate samples
            rows = await pool.fetch(
                "SELECT predicted_rate, rate FROM funding_rates "
                "WHERE venue = $1 AND symbol = $2 "
                "  AND timestamp > NOW() - INTERVAL '20 minutes' "
                "  AND (predicted_rate IS NOT NULL OR rate IS NOT NULL) "
                "ORDER BY timestamp ASC",
                venue, symbol,
            )
            if not rows:
                continue

            # Use predicted_rate where available, fall back to rate
            rates = [
                float(r["predicted_rate"]) if r["predicted_rate"] is not None
                else float(r["rate"])
                for r in rows
            ]

            ema = compute_ema(rates, span=EMA_SPAN)

            # Outlier gate on most recent rate
            current_rate = rates[-1]
            is_outlier_flag = is_outlier(current_rate, ema, rates)
            outlier_flags[(venue, symbol)] = is_outlier_flag

            if not is_outlier_flag:
                ema_data[(venue, symbol)] = ema

    return ema_data, outlier_flags
