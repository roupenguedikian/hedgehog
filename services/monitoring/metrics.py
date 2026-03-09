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
POSITION_FUNDING_ACCRUED = Gauge(
    "hedgehog_position_funding_accrued_usd",
    "Funding accrued for a hedge position",
    ["hedge_id", "symbol", "short_venue", "long_venue"],
)
POSITION_LEVERAGE = Gauge(
    "hedgehog_position_leverage",
    "Effective leverage per position leg",
    ["hedge_id", "symbol", "venue", "side"],
)
POSITION_LIQUIDATION_PRICE = Gauge(
    "hedgehog_position_liquidation_price",
    "Liquidation price per position leg",
    ["hedge_id", "symbol", "venue", "side"],
)
POSITION_UNREALIZED_PNL = Gauge(
    "hedgehog_position_unrealized_pnl_usd",
    "Unrealized P&L per position leg",
    ["hedge_id", "symbol", "venue", "side"],
)
POSITION_ENTRY_PRICE = Gauge(
    "hedgehog_position_entry_price",
    "Entry price per position leg",
    ["hedge_id", "symbol", "venue", "side"],
)
POSITION_MARK_PRICE = Gauge(
    "hedgehog_position_mark_price",
    "Current mark price per position leg",
    ["hedge_id", "symbol", "venue", "side"],
)

# ── Funding rates ────────────────────────────────────────────────────────────

FUNDING_RATE = Gauge(
    "hedgehog_funding_rate_annualized",
    "Annualized funding rate (decimal)",
    ["venue", "symbol"],
)
FUNDING_RATE_RAW = Gauge(
    "hedgehog_funding_rate_raw",
    "Raw funding rate per cycle",
    ["venue", "symbol"],
)
FUNDING_RATE_PREDICTED = Gauge(
    "hedgehog_funding_rate_predicted",
    "Predicted next funding rate (annualized)",
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

# ── Per-venue funding payments ───────────────────────────────────────────────

VENUE_FUNDING_COLLECTED = Gauge(
    "hedgehog_venue_funding_collected_usd",
    "Funding collected on this venue across all positions",
    ["venue"],
)

# ── Market data (per-venue, per-symbol) ──────────────────────────────────────

MARK_PRICE = Gauge(
    "hedgehog_mark_price", "Mark price", ["venue", "symbol"]
)
INDEX_PRICE = Gauge(
    "hedgehog_index_price", "Index price", ["venue", "symbol"]
)
OPEN_INTEREST = Gauge(
    "hedgehog_open_interest_usd", "Open interest in USD", ["venue", "symbol"]
)
ORDERBOOK_SPREAD_BPS = Gauge(
    "hedgehog_orderbook_spread_bps", "Best bid-ask spread in basis points", ["venue", "symbol"]
)
ORDERBOOK_BID_DEPTH_USD = Gauge(
    "hedgehog_orderbook_bid_depth_usd", "Bid depth within 1% of mid price", ["venue", "symbol"]
)
ORDERBOOK_ASK_DEPTH_USD = Gauge(
    "hedgehog_orderbook_ask_depth_usd", "Ask depth within 1% of mid price", ["venue", "symbol"]
)

# ── Venue health ─────────────────────────────────────────────────────────────

VENUE_SCORE = Gauge(
    "hedgehog_venue_score", "Composite venue score (0-1)", ["venue"]
)
VENUE_SCORE_COMPONENT = Gauge(
    "hedgehog_venue_score_component",
    "Individual venue score component",
    ["venue", "component"],
)
VENUE_UP = Gauge(
    "hedgehog_venue_up", "Whether venue is connected (1=up, 0=down)", ["venue"]
)
VENUES_HEALTHY = Gauge("hedgehog_venues_healthy", "Count of healthy venues")
VENUE_API_LATENCY = Histogram(
    "hedgehog_venue_api_latency_seconds",
    "API call latency per venue",
    ["venue", "endpoint"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)

# ── Venue balances ───────────────────────────────────────────────────────────

VENUE_BALANCE_AVAILABLE = Gauge(
    "hedgehog_venue_balance_available_usd",
    "Available (withdrawable) balance on venue",
    ["venue"],
)
VENUE_BALANCE_TOTAL = Gauge(
    "hedgehog_venue_balance_total_usd",
    "Total balance (collateral) on venue",
    ["venue"],
)
VENUE_MARGIN_USED = Gauge(
    "hedgehog_venue_margin_used_usd",
    "Margin currently used on venue",
    ["venue"],
)
VENUE_MARGIN_RATIO = Gauge(
    "hedgehog_venue_margin_ratio",
    "Margin ratio on venue (available / total)",
    ["venue"],
)

# ── Venue exposure ───────────────────────────────────────────────────────────

VENUE_EXPOSURE_PCT = Gauge(
    "hedgehog_venue_exposure_pct",
    "Venue exposure as percentage of NAV",
    ["venue"],
)
VENUE_POSITION_COUNT = Gauge(
    "hedgehog_venue_position_count",
    "Number of position legs on this venue",
    ["venue"],
)
VENUE_NOTIONAL_USD = Gauge(
    "hedgehog_venue_notional_usd",
    "Total notional value of positions on this venue",
    ["venue"],
)

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
VENUE_FEES_PAID_USD = Counter(
    "hedgehog_venue_fees_paid_usd_total",
    "Cumulative trading fees paid on venue",
    ["venue"],
)
VENUE_GAS_PAID_USD = Counter(
    "hedgehog_venue_gas_paid_usd_total",
    "Cumulative gas paid on venue",
    ["venue"],
)

# ── Orders ───────────────────────────────────────────────────────────────────

OPEN_ORDERS = Gauge(
    "hedgehog_open_orders",
    "Number of currently open orders on venue",
    ["venue"],
)
ORDER_FILLS_TOTAL = Counter(
    "hedgehog_order_fills_total",
    "Total order fills",
    ["venue", "symbol", "side", "status"],
)
ORDER_ERRORS_TOTAL = Counter(
    "hedgehog_order_errors_total",
    "Total order errors",
    ["venue", "error_type"],
)
ORDER_SLIPPAGE_BPS = Histogram(
    "hedgehog_order_slippage_bps",
    "Order slippage in basis points",
    ["venue"],
    buckets=[0, 1, 2, 5, 10, 20, 50, 100],
)
ORDER_FILL_TIME = Histogram(
    "hedgehog_order_fill_time_seconds",
    "Time to fill an order",
    ["venue"],
    buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
)
ROLLBACKS_TOTAL = Counter(
    "hedgehog_rollbacks_total",
    "Total rollback events",
    ["venue"],
)

# ── Funding rate statistics ──────────────────────────────────────────────────

FUNDING_RATE_MEAN_24H = Gauge(
    "hedgehog_funding_rate_mean_24h",
    "Mean annualized funding rate over last 24h",
    ["venue", "symbol"],
)
FUNDING_RATE_STD_24H = Gauge(
    "hedgehog_funding_rate_std_24h",
    "Std dev of funding rate over last 24h",
    ["venue", "symbol"],
)
FUNDING_RATE_FLIPS_24H = Gauge(
    "hedgehog_funding_rate_flips_24h",
    "Number of funding rate sign flips in last 24h",
    ["venue", "symbol"],
)


def start_metrics_server(port: int = 8000) -> None:
    """Start the Prometheus metrics HTTP server."""
    start_http_server(port)
