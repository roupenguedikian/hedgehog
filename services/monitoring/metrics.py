"""
hedgehog/services/monitoring/metrics.py
No-op metric stubs — Prometheus removed, web UI reads from TimescaleDB.

Every attribute accessed on a stub returns itself, so chained calls like
  m.FUNDING_RATE.labels(venue=v, symbol=s).set(val)
silently do nothing.
"""
from __future__ import annotations


class _Noop:
    """Swallows any attribute access or method call."""
    def __getattr__(self, _):
        return self
    def __call__(self, *a, **kw):
        return self


_noop = _Noop()

# All metric names used across the codebase — each is a silent no-op.
PORTFOLIO_NAV = _noop
PORTFOLIO_PNL = _noop
PORTFOLIO_FUNDING_COLLECTED = _noop
ACTIVE_POSITIONS = _noop
POSITION_SIZE = _noop
POSITION_PNL = _noop
POSITION_FUNDING_ACCRUED = _noop
POSITION_LEVERAGE = _noop
POSITION_LIQUIDATION_PRICE = _noop
POSITION_UNREALIZED_PNL = _noop
POSITION_ENTRY_PRICE = _noop
POSITION_MARK_PRICE = _noop
FUNDING_RATE = _noop
FUNDING_RATE_RAW = _noop
FUNDING_RATE_PREDICTED = _noop
BEST_SPREAD = _noop
OPPORTUNITIES_FOUND = _noop
VENUE_FUNDING_COLLECTED = _noop
MARK_PRICE = _noop
INDEX_PRICE = _noop
OPEN_INTEREST = _noop
ORDERBOOK_SPREAD_BPS = _noop
ORDERBOOK_BID_DEPTH_USD = _noop
ORDERBOOK_ASK_DEPTH_USD = _noop
VENUE_SCORE = _noop
VENUE_SCORE_COMPONENT = _noop
VENUE_UP = _noop
VENUES_HEALTHY = _noop
VENUE_API_LATENCY = _noop
VENUE_BALANCE_AVAILABLE = _noop
VENUE_BALANCE_TOTAL = _noop
VENUE_MARGIN_USED = _noop
VENUE_MARGIN_RATIO = _noop
VENUE_EXPOSURE_PCT = _noop
VENUE_POSITION_COUNT = _noop
VENUE_NOTIONAL_USD = _noop
RISK_DRAWDOWN_PCT = _noop
RISK_MAX_VENUE_EXPOSURE_PCT = _noop
RISK_CHAIN_CONCENTRATION_PCT = _noop
RISK_MARGIN_UTILIZATION_PCT = _noop
RISK_ORACLE_DIVERGENCE_PCT = _noop
RISK_BRIDGE_TRANSIT_PCT = _noop
CIRCUIT_BREAKER_TRIGGERED = _noop
CYCLE_COUNT = _noop
CYCLE_DURATION = _noop
TRADES_TOTAL = _noop
TRADE_DECISIONS = _noop
FEES_PAID_USD = _noop
GAS_PAID_USD = _noop
VENUE_FEES_PAID_USD = _noop
VENUE_GAS_PAID_USD = _noop
OPEN_ORDERS = _noop
ORDER_FILLS_TOTAL = _noop
ORDER_ERRORS_TOTAL = _noop
ORDER_SLIPPAGE_BPS = _noop
ORDER_FILL_TIME = _noop
ROLLBACKS_TOTAL = _noop
FUNDING_RATE_MEAN_24H = _noop
FUNDING_RATE_STD_24H = _noop
FUNDING_RATE_FLIPS_24H = _noop


def start_metrics_server(port: int = 8000) -> None:
    """No-op — Prometheus removed."""
    pass
