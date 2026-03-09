"""
hedgehog/models/core.py
Core data models for the funding rate hedge bot.
All models are Pydantic for validation + serialization.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field


# ── Enums ────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

class ActionType(str, Enum):
    ENTER_HEDGE = "ENTER_HEDGE"
    EXIT_HEDGE = "EXIT_HEDGE"
    ROTATE = "ROTATE"
    HOLD = "HOLD"
    REBALANCE = "REBALANCE"

class RiskDecision(str, Enum):
    APPROVE = "APPROVE"
    RESIZE = "RESIZE"
    REJECT = "REJECT"
    HALT = "HALT"

class VenueTier(str, Enum):
    TIER_1 = "tier_1"  # Battle-tested, high TVL
    TIER_2 = "tier_2"  # Strong tech, growing
    TIER_3 = "tier_3"  # Newer or less proven

class ChainType(str, Enum):
    EVM = "evm"
    SOLANA = "solana"
    COSMOS = "cosmos"
    STARKNET = "starknet"

class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# ── Funding Rate ─────────────────────────────────────────────────────────────

class FundingRate(BaseModel):
    venue: str
    symbol: str
    rate: float                              # raw rate per cycle (e.g., 0.0001 = 0.01%)
    cycle_hours: int                         # 1, 4, or 8
    mark_price: float = 0.0
    index_price: float = 0.0
    next_funding_ts: Optional[datetime] = None
    predicted_rate: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def annualized(self) -> float:
        """Annualized rate: rate_per_cycle * cycles_per_year."""
        cycles_per_year = 8760 / self.cycle_hours
        return self.rate * cycles_per_year

    @computed_field
    @property
    def annualized_pct(self) -> float:
        return self.annualized * 100


# ── Venue Scoring ────────────────────────────────────────────────────────────

class VenueScore(BaseModel):
    venue: str
    symbol: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Raw metrics
    avg_funding_rate_30d: float = 0.0        # annualized
    funding_rate_std_30d: float = 0.0        # volatility of rate
    liquidity_depth_1pct_usd: float = 0.0    # USD to move price 1%
    trading_fee_bps: float = 0.0             # round-trip fee in bps
    funding_cycle_hours: int = 8
    contract_age_months: float = 0.0
    uptime_30d: float = 1.0                  # 0.0 to 1.0

    # Computed composite score
    composite_score: float = 0.0

    # Optional enrichments
    open_interest_usd: float = 0.0
    daily_volume_usd: float = 0.0


class VenuePairOpportunity(BaseModel):
    """A funding rate arbitrage opportunity between two venues."""
    symbol: str
    short_venue: str                          # venue to short (collecting funding)
    long_venue: str                           # venue to long (paying less funding)
    short_rate_annual: float
    long_rate_annual: float
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def spread_annual(self) -> float:
        return self.short_rate_annual - self.long_rate_annual

    @computed_field
    @property
    def spread_pct(self) -> float:
        return self.spread_annual * 100


# ── Orderbook ────────────────────────────────────────────────────────────────

class OrderbookLevel(BaseModel):
    price: float
    size: float

class Orderbook(BaseModel):
    venue: str
    symbol: str
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def spread_bps(self) -> float:
        if not self.bids or not self.asks:
            return float("inf")
        return ((self.asks[0].price - self.bids[0].price) / self.bids[0].price) * 10000

    def depth_at_pct(self, pct: float = 1.0) -> dict:
        """USD depth within `pct`% of mid price."""
        if not self.bids or not self.asks:
            return {"bid_depth": 0, "ask_depth": 0}
        mid = (self.bids[0].price + self.asks[0].price) / 2
        bid_threshold = mid * (1 - pct / 100)
        ask_threshold = mid * (1 + pct / 100)
        bid_depth = sum(l.price * l.size for l in self.bids if l.price >= bid_threshold)
        ask_depth = sum(l.price * l.size for l in self.asks if l.price <= ask_threshold)
        return {"bid_depth": bid_depth, "ask_depth": ask_depth}


# ── Positions ────────────────────────────────────────────────────────────────

class Position(BaseModel):
    venue: str
    symbol: str
    side: Side
    size: float                              # in base asset units
    size_usd: float = 0.0
    entry_price: float
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    margin: float = 0.0
    leverage: float = 1.0
    funding_accrued: float = 0.0
    liquidation_price: float = 0.0
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HedgePosition(BaseModel):
    """A paired hedge: short on one venue, long on another."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    symbol: str
    short_leg: Position
    long_leg: Position
    total_fees: float = 0.0                  # cumulative trading fees both legs
    total_gas: float = 0.0                   # cumulative gas costs both legs
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def net_size_usd(self) -> float:
        return (self.short_leg.size_usd + self.long_leg.size_usd) / 2

    @computed_field
    @property
    def net_unrealized_pnl(self) -> float:
        return self.short_leg.unrealized_pnl + self.long_leg.unrealized_pnl

    @computed_field
    @property
    def total_funding_accrued(self) -> float:
        return self.short_leg.funding_accrued + self.long_leg.funding_accrued

    @computed_field
    @property
    def entry_basis(self) -> float:
        return self.short_leg.entry_price - self.long_leg.entry_price


# ── Orders ───────────────────────────────────────────────────────────────────

class OrderResult(BaseModel):
    venue: str
    symbol: str
    side: Side
    status: OrderStatus
    order_id: str = ""
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    fee_currency: str = "USD"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tx_hash: str = ""
    error: Optional[str] = None


# ── Bridge / Capital ─────────────────────────────────────────────────────────

class BridgeRoute(BaseModel):
    from_chain: str
    to_chain: str
    from_venue: str
    to_venue: str
    bridge_id: str                           # e.g., "hyperliquid_native", "lifi", "ibc"
    estimated_fee_usd: float
    estimated_time_seconds: int
    amount_usd: float
    token: str = "USDC"


class PortfolioSnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_nav: float = 0.0
    positions: list[HedgePosition] = []
    venue_balances: dict[str, float] = {}    # venue -> available balance USD
    in_transit: float = 0.0                  # capital currently bridging
    total_funding_collected: float = 0.0
    total_realized_pnl: float = 0.0
    peak_nav: float = 0.0

    @computed_field
    @property
    def drawdown_pct(self) -> float:
        if self.peak_nav <= 0:
            return 0.0
        return ((self.peak_nav - self.total_nav) / self.peak_nav) * 100


# ── Risk ─────────────────────────────────────────────────────────────────────

class RiskCheck(BaseModel):
    name: str
    status: str                              # "OK", "WARNING", "CRITICAL"
    value: float = 0.0
    limit: float = 0.0
    message: str = ""

class RiskReport(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    checks: list[RiskCheck] = []
    halt: bool = False
    overall_status: str = "OK"


# ── Agent Messages ───────────────────────────────────────────────────────────

class TradeAction(BaseModel):
    """A proposed trade action from the strategist agent."""
    action_type: ActionType
    symbol: str
    short_venue: str = ""
    long_venue: str = ""
    size_usd: float = 0.0
    expected_annual_yield: float = 0.0
    confidence: float = 0.5                  # 0.0 to 1.0
    reasoning: str = ""
    position_id: str = ""                    # for EXIT/ROTATE, which position


class AgentMessage(BaseModel):
    agent_id: str
    action_type: str                         # PROPOSE, APPROVE, REJECT, REPORT, ESCALATE
    payload: dict = {}
    reasoning: str = ""
    confidence: float = 0.5
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Venue Configuration ─────────────────────────────────────────────────────

class VenueConfig(BaseModel):
    name: str
    chain: str
    chain_type: ChainType
    settlement_chain: str = ""               # where final settlement happens
    funding_cycle_hours: int = 1
    maker_fee_bps: float = 0.0
    taker_fee_bps: float = 0.0
    max_leverage: int = 20
    collateral_token: str = "USDC"
    has_api: bool = True
    api_base_url: str = ""
    ws_url: str = ""
    deposit_chain: str = ""
    tier: VenueTier = VenueTier.TIER_2
    zero_gas: bool = False
    supports_symbols: list[str] = []
    symbol_format: str = "{symbol}"          # e.g., "{symbol}-USD-PERP" for Paradex
    symbol_overrides: dict[str, str] = {}    # manual overrides per symbol

    # DeFi-specific
    has_escape_hatch: bool = False           # can force-withdraw if sequencer down
    has_privacy: bool = False                # hidden orders/positions
    has_anti_mev: bool = False               # FBA or similar
    yield_bearing_collateral: bool = False   # e.g., Ethereal USDe
