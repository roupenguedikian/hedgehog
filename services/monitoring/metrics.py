"""
hedgehog/services/monitoring/metrics.py
Prometheus metrics exporter for the HedgeHog bot.

Exposes metrics on :8000/metrics for Prometheus scraping.
"""
from __future__ import annotations

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    start_http_server,
)

# ── Bot info ─────────────────────────────────────────────────────────────────

BOT_INFO = Info("hedgehog", "HedgeHog bot metadata")
BOT_INFO.info({"version": "1.0", "project": "aegis_protocol"})

# ── Portfolio ────────────────────────────────────────────────────────────────

PORTFOLIO_NAV = Gauge("hedgehog_portfolio_nav_usd", "Total portfolio NAV in USD")
PORTFOLIO_PNL = Gauge("hedgehog_portfolio_pnl_usd", "Cumulative net P&L in USD")
PORTFOLIO_FUNDING_COLLECTED = Gauge(
    "hedgehog_portfolio_funding_collected_usd",
    "Total funding collected across all positions",
)

# ── Positions ────────────────────────────────────────────────────────────────

ACTIVE_POSITIONS = Gauge("hedgehog_active_positions", "Number of active hedge positions")
POSITION_SIZE = Gauge(
    "hedgehog_position_size_usd",
    "Position size in USD",
    ["hedge_id", "symbol", "short_venue", "long_venue"],
)
POSITION_PNL = Gauge(
    "hedgehog_position_pnl_usd",
    "Position net P&L in USD",
    ["hedge_id", "symbol"],
)

# ── Funding rates ────────────────────────────────────────────────────────────

FUNDING_RATE = Gauge(
    "hedgehog_funding_rate_annualized",
    "Annualized funding rate (decimal)",
    ["venue", "symbol"],
)
BEST_SPREAD = Gauge(
    "hedgehog_best_spread_annualized",
    "Best available spread (decimal)",
    ["symbol", "short_venue", "long_venue"],
)
OPPORTUNITIES_FOUND = Gauge(
    "hedgehog_opportunities_found", "Number of opportunities above threshold"
)

# ── Venue health ─────────────────────────────────────────────────────────────

VENUE_SCORE = Gauge(
    "hedgehog_venue_score", "Composite venue score (0-1)", ["venue"]
)
VENUE_UP = Gauge(
    "hedgehog_venue_up", "Whether venue is connected (1=up, 0=down)", ["venue"]
)
VENUES_HEALTHY = Gauge("hedgehog_venues_healthy", "Count of healthy venues")

# ── Risk ─────────────────────────────────────────────────────────────────────

RISK_DRAWDOWN_PCT = Gauge("hedgehog_risk_drawdown_pct", "Current drawdown percentage")
RISK_MAX_VENUE_EXPOSURE_PCT = Gauge(
    "hedgehog_risk_max_venue_exposure_pct", "Max venue exposure percentage"
)
RISK_CHAIN_CONCENTRATION_PCT = Gauge(
    "hedgehog_risk_chain_concentration_pct", "Max chain concentration percentage"
)
RISK_MARGIN_UTILIZATION_PCT = Gauge(
    "hedgehog_risk_margin_utilization_pct", "Margin utilization percentage"
)
RISK_ORACLE_DIVERGENCE_PCT = Gauge(
    "hedgehog_risk_oracle_divergence_pct", "Oracle price divergence percentage"
)
RISK_BRIDGE_TRANSIT_PCT = Gauge(
    "hedgehog_risk_bridge_transit_pct", "NAV percentage in bridge transit"
)
CIRCUIT_BREAKER_TRIGGERED = Gauge(
    "hedgehog_circuit_breaker_triggered", "Whether circuit breaker is active (1=yes)"
)

# ── Strategy loop ────────────────────────────────────────────────────────────

CYCLE_COUNT = Counter("hedgehog_cycles_total", "Total strategy cycles completed")
CYCLE_DURATION = Histogram(
    "hedgehog_cycle_duration_seconds",
    "Time to complete one strategy cycle",
    buckets=[0.5, 1, 2, 5, 10, 30, 60],
)

# ── Trades ───────────────────────────────────────────────────────────────────

TRADES_TOTAL = Counter(
    "hedgehog_trades_total", "Total trades executed", ["action", "venue"]
)
TRADE_DECISIONS = Counter(
    "hedgehog_trade_decisions_total",
    "Risk decisions on proposed trades",
    ["decision"],
)
FEES_PAID_USD = Counter("hedgehog_fees_paid_usd_total", "Cumulative fees paid in USD")
GAS_PAID_USD = Counter("hedgehog_gas_paid_usd_total", "Cumulative gas paid in USD")


def start_metrics_server(port: int = 8000) -> None:
    """Start the Prometheus metrics HTTP server."""
    start_http_server(port)
