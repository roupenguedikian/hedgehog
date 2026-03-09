# Hedgehog — Code-Level Changes

Concrete changes organized by priority. Each section includes the file, what to change, and why.

---

## 1. Funding Rate Data Validation (services/data/funding_collector.py)

The collector pulls from venue adapters, CoinGlass, and DefiLlama with no cross-source sanity checks. A stale or erroneous rate from one source feeds directly into the VenueScorer and then into the LLM strategy layer.

**Add a `_validate_rate` method and call it in `collect_once`:**

```python
# services/data/funding_collector.py — add to FundingRateCollector class

RATE_SANITY_BOUNDS = {
    "min_annualized": -5.0,   # -500% annual is absurd
    "max_annualized": 5.0,    # +500% annual is absurd
    "max_cross_source_divergence": 0.10,  # 10% annualized disagreement
}

def _validate_rate(self, rate: FundingRate) -> bool:
    """Reject rates that are stale, out of bounds, or diverge from other sources."""
    from datetime import datetime, timezone, timedelta

    # 1. Staleness — reject data older than threshold from risk.yaml
    age = (datetime.now(timezone.utc) - rate.timestamp).total_seconds()
    if age > 300:  # stale_data_threshold_sec from config
        logger.warning("funding.stale_rate",
                       venue=rate.venue, symbol=rate.symbol, age_sec=age)
        return False

    # 2. Bounds — reject nonsensical rates
    if not (self.RATE_SANITY_BOUNDS["min_annualized"]
            <= rate.annualized
            <= self.RATE_SANITY_BOUNDS["max_annualized"]):
        logger.warning("funding.rate_out_of_bounds",
                       venue=rate.venue, symbol=rate.symbol,
                       annualized=rate.annualized)
        return False

    # 3. Cross-source divergence — compare against other venues for same symbol
    other_rates = [
        r.annualized for (v, s), r in self.latest_rates.items()
        if s == rate.symbol and v != rate.venue
    ]
    if other_rates:
        median = sorted(other_rates)[len(other_rates) // 2]
        divergence = abs(rate.annualized - median)
        if divergence > self.RATE_SANITY_BOUNDS["max_cross_source_divergence"]:
            logger.warning("funding.rate_divergence",
                           venue=rate.venue, symbol=rate.symbol,
                           rate=rate.annualized, median=median,
                           divergence=divergence)
            # Don't reject outright — flag it and let scorer downweight
            rate.extra = getattr(rate, 'extra', {})
            rate.extra["divergence_flag"] = True

    return True
```

Then gate every rate insertion in `collect_once`:

```python
# In collect_once, where rates are stored into self.latest_rates:
if self._validate_rate(rate):
    self.latest_rates[(rate.venue, rate.symbol)] = rate
    self.rate_history.append(rate)
else:
    logger.info("funding.rate_rejected", venue=rate.venue, symbol=rate.symbol)
```

---

## 2. LLM Fallback Strategy (new file: services/strategy/fallback_strategy.py)

When the Claude API is down, the bot currently just stalls. Add a deterministic heuristic that can keep the bot productive.

```python
"""
hedgehog/services/strategy/fallback_strategy.py
Simple threshold-based strategy for when Claude API is unavailable.
No LLM calls — pure math on collector + scorer outputs.
"""
from __future__ import annotations

import structlog
from models.core import TradeAction, ActionType

logger = structlog.get_logger()


class FallbackStrategy:
    """
    Deterministic funding rate arb strategy.
    Used when Claude API is unreachable or latency exceeds threshold.

    Rules:
    - Only ENTER_HEDGE when spread > min_spread AND both venues score > min_score
    - Only use Tier 1 + Tier 2 venues (no Tier 3 in fallback mode)
    - Position size = fixed fraction of NAV (no Kelly, conservative)
    - Only trade BTC and ETH (most liquid, lowest execution risk)
    """

    FALLBACK_SYMBOLS = ["BTC", "ETH"]
    FALLBACK_SIZE_PCT = 0.05  # 5% of NAV per position, very conservative
    MIN_VENUE_SCORE = 0.4
    ALLOWED_TIERS = {"tier_1", "tier_2"}

    def __init__(self, collector, scorer, venue_configs: dict,
                 min_spread_annual: float = 0.12):
        self.collector = collector
        self.scorer = scorer
        self.venue_configs = venue_configs
        self.min_spread = min_spread_annual

    def generate_actions(self, portfolio_nav: float,
                         existing_positions: dict) -> list[TradeAction]:
        actions = []

        for opp in self.collector.latest_opportunities:
            if opp.symbol not in self.FALLBACK_SYMBOLS:
                continue
            if opp.spread_annual < self.min_spread:
                continue

            # Check venue tiers
            short_vc = self.venue_configs.get(opp.short_venue)
            long_vc = self.venue_configs.get(opp.long_venue)
            if not short_vc or not long_vc:
                continue
            if short_vc.tier.value not in self.ALLOWED_TIERS:
                continue
            if long_vc.tier.value not in self.ALLOWED_TIERS:
                continue

            # Check venue scores
            short_score = self.scorer.get_score(opp.short_venue, opp.symbol)
            long_score = self.scorer.get_score(opp.long_venue, opp.symbol)
            if not short_score or short_score.composite_score < self.MIN_VENUE_SCORE:
                continue
            if not long_score or long_score.composite_score < self.MIN_VENUE_SCORE:
                continue

            # Skip if we already have a position on either venue for this symbol
            for pos in existing_positions.values():
                if (pos.symbol == opp.symbol and
                    (pos.short_leg.venue == opp.short_venue or
                     pos.long_leg.venue == opp.long_venue)):
                    break
            else:
                size = portfolio_nav * self.FALLBACK_SIZE_PCT
                actions.append(TradeAction(
                    action_type=ActionType.ENTER_HEDGE,
                    symbol=opp.symbol,
                    short_venue=opp.short_venue,
                    long_venue=opp.long_venue,
                    size_usd=size,
                    expected_annual_yield=opp.spread_annual,
                    confidence=0.6,  # lower confidence in fallback mode
                    reasoning=f"[FALLBACK] spread={opp.spread_annual*100:.1f}% "
                              f"short={opp.short_venue} long={opp.long_venue}",
                ))

            if len(actions) >= 2:  # max 2 new positions per cycle in fallback
                break

        return actions
```

**Wire it into the BotEngine in main.py:**

```python
# In run_full_bot, after creating tools and before BotEngine class:
from services.strategy.fallback_strategy import FallbackStrategy

fallback = FallbackStrategy(
    collector=collector,
    scorer=scorer,
    venue_configs=venue_configs,
    min_spread_annual=configs["strategy"].get("min_spread_annual", 0.10) + 0.02,
    # Slightly higher threshold in fallback — be more conservative
)

# Inside BotEngine.run_cycle, wrap the LLM call:
async def run_cycle(self):
    # ... collect, score as before ...

    try:
        # Existing LLM strategy call with a timeout
        actions = await asyncio.wait_for(
            self._llm_strategy_cycle(opportunities),
            timeout=30.0
        )
        self._llm_consecutive_failures = 0
    except (asyncio.TimeoutError, Exception) as e:
        self._llm_consecutive_failures = getattr(
            self, '_llm_consecutive_failures', 0) + 1
        logger.warning("bot.llm_unavailable",
                       error=str(e),
                       consecutive=self._llm_consecutive_failures)

        if self._llm_consecutive_failures >= 2:
            logger.info("bot.using_fallback_strategy")
            actions = fallback.generate_actions(
                portfolio_nav=self.portfolio.total_nav,
                existing_positions=execution_engine.active_positions,
            )
        else:
            actions = []  # skip one cycle, retry LLM next time
```

---

## 3. Execution Engine Hardening (services/capital/execution_engine.py)

The rollback logic has a critical gap: if the rollback market order itself fails, you're left with an unhedged position and only a log line. The circuit breaker may not catch it immediately because it monitors portfolio-level drawdown, not per-position hedge integrity.

**Add retry + circuit breaker notification to `_rollback`:**

```python
async def _rollback(self, short_result, long_result,
                    short_adapter, long_adapter,
                    short_symbol, long_symbol):
    """Rollback with retries. If rollback fails, alert circuit breaker."""
    MAX_RETRIES = 3
    unhedged = False

    for attempt in range(MAX_RETRIES):
        try:
            if (short_result and isinstance(short_result, OrderResult)
                    and short_result.filled_qty > 0):
                logger.info("execution.rollback_short",
                            qty=short_result.filled_qty, attempt=attempt)
                result = await asyncio.wait_for(
                    short_adapter.place_market_order(
                        short_symbol, Side.LONG,
                        short_result.filled_qty, reduce_only=True),
                    timeout=ORDER_TIMEOUT_SECONDS,
                )
                if result.status == OrderStatus.FILLED:
                    short_result = None  # successfully rolled back
        except Exception as e:
            logger.error("execution.rollback_short_failed",
                         attempt=attempt, error=str(e))

        try:
            if (long_result and isinstance(long_result, OrderResult)
                    and long_result.filled_qty > 0):
                logger.info("execution.rollback_long",
                            qty=long_result.filled_qty, attempt=attempt)
                result = await asyncio.wait_for(
                    long_adapter.place_market_order(
                        long_symbol, Side.SHORT,
                        long_result.filled_qty, reduce_only=True),
                    timeout=ORDER_TIMEOUT_SECONDS,
                )
                if result.status == OrderStatus.FILLED:
                    long_result = None  # successfully rolled back
        except Exception as e:
            logger.error("execution.rollback_long_failed",
                         attempt=attempt, error=str(e))

        # Check if both sides are clean
        remaining_short = (short_result and isinstance(short_result, OrderResult)
                          and short_result.filled_qty > 0)
        remaining_long = (long_result and isinstance(long_result, OrderResult)
                         and long_result.filled_qty > 0)

        if not remaining_short and not remaining_long:
            logger.info("execution.rollback_complete", attempts=attempt + 1)
            return

        await asyncio.sleep(0.5 * (attempt + 1))  # backoff

    # Rollback exhausted — we have an unhedged position
    logger.critical("execution.ROLLBACK_FAILED_UNHEDGED",
                    short_remaining=bool(short_result),
                    long_remaining=bool(long_result))
    # Store for manual intervention / circuit breaker awareness
    self._unhedged_positions.append({
        "short": short_result,
        "long": long_result,
        "short_symbol": short_symbol,
        "long_symbol": long_symbol,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
```

**Add `_unhedged_positions` tracking to `__init__`:**

```python
def __init__(self, adapters: dict):
    self.adapters = adapters
    self.active_positions: dict[str, HedgePosition] = {}
    self._unhedged_positions: list[dict] = []  # failed rollbacks

@property
def has_unhedged_exposure(self) -> bool:
    return len(self._unhedged_positions) > 0
```

**Add a timeout to the dual-leg `asyncio.gather`:**

```python
# In enter_hedge, replace the bare gather with a timeout:
try:
    results = await asyncio.wait_for(
        asyncio.gather(short_task, long_task, return_exceptions=True),
        timeout=ORDER_TIMEOUT_SECONDS,
    )
except asyncio.TimeoutError:
    logger.error("execution.dual_leg_timeout",
                 symbol=symbol, short=short_venue, long=long_venue)
    # Try to cancel any pending orders
    try:
        await short_adapter.cancel_all_orders()
    except Exception:
        pass
    try:
        await long_adapter.cancel_all_orders()
    except Exception:
        pass
    return {"status": "TIMEOUT", "reason": "Dual-leg execution timed out"}
```

---

## 4. Circuit Breaker — Check for Unhedged Exposure (services/risk/circuit_breaker.py)

The circuit breaker currently only checks drawdown and absolute loss. It should also trigger on unhedged positions from failed rollbacks.

```python
# Add to CircuitBreaker.__init__:
self.execution_engine = None  # set after construction

# Add a new check method:
async def check(self, portfolio: PortfolioSnapshot) -> bool:
    if self.triggered:
        return True

    # Existing checks ...

    # NEW: Unhedged exposure check
    if (self.execution_engine
            and self.execution_engine.has_unhedged_exposure):
        await self._trigger(
            f"Unhedged exposure detected: "
            f"{len(self.execution_engine._unhedged_positions)} failed rollbacks"
        )
        return True

    return False
```

**Wire it in `run_full_bot`:**

```python
# After creating both execution_engine and circuit_breaker:
circuit_breaker.execution_engine = execution_engine
```

---

## 5. Reconcile Wallet Management (main.py)

`main.py` reads keys from env vars and passes raw private key strings to adapters. The `WalletManager` exists but isn't used. Consolidate to a single path.

**Replace the env-var key loading in main.py with WalletManager:**

```python
# In run_full_bot (and other run_* functions), replace:
#   key = os.environ.get(f"{venue.upper()}_PRIVATE_KEY", os.environ.get("EVM_PRIVATE_KEY"))
#   adapter.set_private_key(key)

# With:
from services.wallet.wallet_manager import WalletManager

wallet_mgr = WalletManager(
    mnemonic=os.environ.get("MASTER_MNEMONIC", ""),
    private_keys={
        "evm": os.environ.get("EVM_PRIVATE_KEY", ""),
        "solana": os.environ.get("DRIFT_PRIVATE_KEY", ""),
    }
)

# During adapter connection:
async def connect_adapters(adapters, venue_configs, wallet_mgr):
    connected = {}
    for name, adapter in adapters.items():
        try:
            wallet = wallet_mgr.get_wallet_for_venue(name)
            adapter.set_wallet(wallet)  # new method on BaseDefiAdapter
            await adapter.connect()
            if adapter.connected:
                connected[name] = adapter
        except Exception as e:
            logger.warning("adapter.connect_failed", venue=name, error=str(e))
    return connected
```

**Add `set_wallet` to `BaseDefiAdapter`:**

```python
# adapters/base_adapter.py
def set_wallet(self, wallet: "WalletKeys"):
    """Set wallet credentials for this adapter."""
    self._wallet = wallet
    # Backward compat — adapters that use self.private_key directly
    self.private_key = wallet.private_key
    self.address = wallet.address
```

**Update `.env.example`** — deprecate per-venue keys, add mnemonic:

```bash
# Preferred — single mnemonic derives all venue keys
MASTER_MNEMONIC=

# Legacy fallbacks (used if mnemonic is empty)
EVM_PRIVATE_KEY=
DRIFT_PRIVATE_KEY=
```

---

## 6. Risk Engine — Read Limits from Config, Not Hardcode (services/risk/risk_engine.py)

Position size limit is hardcoded to 20% instead of reading from config. The margin utilization check (config key `max_margin_utilization: 0.60`) isn't implemented at all.

```python
# In evaluate_trade, replace:
#   max_pos_pct = 20.0
# With:
max_pos_pct = self.config.get("max_single_position_pct", 20.0)

# Add a new check for margin utilization (currently missing entirely):
# After the existing checks, add:

# 7. Margin utilization
max_margin_util = self.config.get("max_margin_utilization", 0.60)
if portfolio.margin_used > 0 and portfolio.total_nav > 0:
    current_util = portfolio.margin_used / portfolio.total_nav
    projected_util = current_util + (action.size_usd * 0.1 / portfolio.total_nav)
    if projected_util > max_margin_util:
        checks.append(RiskCheck(
            name="margin_utilization",
            status="CRITICAL",
            value=projected_util * 100,
            limit=max_margin_util * 100,
            message=f"Margin util {projected_util*100:.1f}% > {max_margin_util*100:.0f}% limit",
        ))

# 8. Stale data check
stale_threshold = self.config.get("stale_data_threshold_sec", 300)
# (requires passing last_data_timestamp through TradeAction or portfolio)
```

**Add `margin_used` to `PortfolioSnapshot` if not present:**

```python
# models/core.py — in PortfolioSnapshot
class PortfolioSnapshot(BaseModel):
    total_nav: float = 0.0
    drawdown_pct: float = 0.0
    margin_used: float = 0.0       # ADD THIS
    positions: list = Field(default_factory=list)
    # ...
```

---

## 7. Add `__init__.py` for Missing Packages

`services/bridge/` and `services/strategy/` need proper init files so imports work.

```python
# services/strategy/__init__.py
"""Strategy module — LLM-driven and fallback strategies."""

# services/bridge/__init__.py
"""Bridge routing module — placeholder for cross-chain capital movement."""
```

---

## 8. Test Skeleton (tests/)

The assessment correctly flags an empty test suite as the top priority. Here's the critical-path test structure to build first.

**tests/test_risk_engine.py** — most important because it guards real money:

```python
"""Tests for risk engine limit enforcement."""
import pytest
from models.core import (
    TradeAction, ActionType, PortfolioSnapshot, RiskDecision,
    VenueConfig, VenueTier, ChainType,
)
from services.risk.risk_engine import RiskEngine


@pytest.fixture
def risk_config():
    return {
        "max_drawdown_pct": 5.0,
        "max_single_venue_pct": 25.0,
        "max_single_chain_pct": 40.0,
        "max_margin_utilization": 0.60,
        "tier_limits": {"tier_1": 0.35, "tier_2": 0.25, "tier_3": 0.15},
    }


@pytest.fixture
def venue_configs():
    return {
        "hyperliquid": VenueConfig(
            name="hyperliquid", chain=ChainType.EVM,
            tier=VenueTier.TIER_1, taker_fee_bps=1.0,
            funding_cycle_hours=1,
        ),
        "drift": VenueConfig(
            name="drift", chain=ChainType.SOLANA,
            tier=VenueTier.TIER_1, taker_fee_bps=2.0,
            funding_cycle_hours=1,
        ),
    }


class TestRiskEngine:
    def test_rejects_when_drawdown_exceeded(self, risk_config, venue_configs):
        engine = RiskEngine(risk_config, venue_configs)
        portfolio = PortfolioSnapshot(total_nav=100000, drawdown_pct=6.0)
        action = TradeAction(
            action_type=ActionType.ENTER_HEDGE,
            symbol="BTC", short_venue="hyperliquid",
            long_venue="drift", size_usd=10000,
            expected_annual_yield=0.15, confidence=0.8,
        )
        decision, checks = engine.evaluate_trade(action, portfolio)
        assert decision == RiskDecision.REJECT

    def test_warns_on_oversized_position(self, risk_config, venue_configs):
        engine = RiskEngine(risk_config, venue_configs)
        portfolio = PortfolioSnapshot(total_nav=50000)
        action = TradeAction(
            action_type=ActionType.ENTER_HEDGE,
            symbol="BTC", short_venue="hyperliquid",
            long_venue="drift", size_usd=15000,  # 30% of NAV
            expected_annual_yield=0.15, confidence=0.8,
        )
        decision, checks = engine.evaluate_trade(action, portfolio)
        position_check = next(c for c in checks if c.name == "position_size")
        assert position_check.status == "WARNING"

    def test_approves_valid_trade(self, risk_config, venue_configs):
        engine = RiskEngine(risk_config, venue_configs)
        portfolio = PortfolioSnapshot(total_nav=100000, drawdown_pct=1.0)
        action = TradeAction(
            action_type=ActionType.ENTER_HEDGE,
            symbol="BTC", short_venue="hyperliquid",
            long_venue="drift", size_usd=10000,
            expected_annual_yield=0.20, confidence=0.8,
        )
        decision, checks = engine.evaluate_trade(action, portfolio)
        assert decision == RiskDecision.APPROVE
```

**tests/test_circuit_breaker.py:**

```python
"""Tests for circuit breaker trigger conditions."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from models.core import PortfolioSnapshot
from services.risk.circuit_breaker import CircuitBreaker


@pytest.fixture
def cb():
    config = {"max_loss_usd": 5000, "max_drawdown_pct": 5.0}
    adapters = {"hyperliquid": AsyncMock()}
    return CircuitBreaker(config, adapters)


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_triggers_on_drawdown(self, cb):
        cb.set_initial_nav(100000)
        portfolio = PortfolioSnapshot(total_nav=94000, drawdown_pct=6.0)
        triggered = await cb.check(portfolio)
        assert triggered is True
        assert cb.triggered is True

    @pytest.mark.asyncio
    async def test_triggers_on_absolute_loss(self, cb):
        cb.set_initial_nav(100000)
        portfolio = PortfolioSnapshot(total_nav=94000, drawdown_pct=3.0)
        triggered = await cb.check(portfolio)
        assert triggered is True  # loss = $6000 > $5000

    @pytest.mark.asyncio
    async def test_no_trigger_within_limits(self, cb):
        cb.set_initial_nav(100000)
        portfolio = PortfolioSnapshot(total_nav=97000, drawdown_pct=2.0)
        triggered = await cb.check(portfolio)
        assert triggered is False

    @pytest.mark.asyncio
    async def test_stays_triggered_once_tripped(self, cb):
        cb.triggered = True
        portfolio = PortfolioSnapshot(total_nav=100000, drawdown_pct=0.0)
        triggered = await cb.check(portfolio)
        assert triggered is True  # latched
```

**tests/test_execution_rollback.py:**

```python
"""Tests for execution engine rollback behavior."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from models.core import OrderResult, OrderStatus, Side
from services.capital.execution_engine import ExecutionEngine


@pytest.fixture
def mock_adapters():
    short = AsyncMock()
    short.connected = True
    short.normalize_symbol = lambda s: s
    short.estimate_gas_cost = lambda _: 0.01

    long = AsyncMock()
    long.connected = True
    long.normalize_symbol = lambda s: s
    long.estimate_gas_cost = lambda _: 0.01

    return {"venue_a": short, "venue_b": long}


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_closes_filled_short(self, mock_adapters):
        engine = ExecutionEngine(mock_adapters)
        short_fill = OrderResult(
            status=OrderStatus.FILLED, filled_qty=1.0,
            avg_price=50000, fee=0.5, order_id="test",
        )
        mock_adapters["venue_a"].place_market_order.return_value = OrderResult(
            status=OrderStatus.FILLED, filled_qty=1.0,
            avg_price=50000, fee=0.5, order_id="rb",
        )
        await engine._rollback(
            short_fill, None,
            mock_adapters["venue_a"], mock_adapters["venue_b"],
            "BTC", "BTC",
        )
        mock_adapters["venue_a"].place_market_order.assert_called_once()
        call_args = mock_adapters["venue_a"].place_market_order.call_args
        assert call_args[0][1] == Side.LONG  # closing a short = buy

    @pytest.mark.asyncio
    async def test_both_failed_no_rollback_needed(self, mock_adapters):
        engine = ExecutionEngine(mock_adapters)
        await engine._rollback(
            None, None,
            mock_adapters["venue_a"], mock_adapters["venue_b"],
            "BTC", "BTC",
        )
        mock_adapters["venue_a"].place_market_order.assert_not_called()
        mock_adapters["venue_b"].place_market_order.assert_not_called()
```

**tests/conftest.py:**

```python
"""Shared fixtures for hedgehog tests."""
import pytest


@pytest.fixture
def sample_portfolio():
    from models.core import PortfolioSnapshot
    return PortfolioSnapshot(total_nav=100000, drawdown_pct=0.0)
```

**pyproject.toml addition (or pytest.ini):**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

---

## 9. Config Reconciliation Checklist

The assessment and CLAUDE.md both note a split: older modules use `config.venues` (Python dict), newer code uses YAML + Pydantic. Here's what to change:

| File | Current Import | Change To |
|------|---------------|-----------|
| `services/risk/risk_engine.py` | `from config.venues import ...` (if present) | Accept `dict` from YAML loader |
| `services/data/venue_scorer.py` | `from config.venues import ...` (if present) | Accept `VenueConfig` from YAML |
| Any module importing `config.venues` | Old-style dict | `models.core.VenueConfig` Pydantic model |

The pattern is already correct in `main.py` — it loads YAML, builds `VenueConfig` objects via `build_venue_configs()`, and passes them down. Any module that imports from `config.venues` directly should be refactored to accept injected config instead.

---

## 10. Metrics for New Checks (services/monitoring/metrics.py)

Add Prometheus gauges for the new safety features:

```python
# Add to services/monitoring/metrics.py:

LLM_FALLBACK_ACTIVE = Gauge(
    "hedgehog_llm_fallback_active",
    "Whether the bot is using fallback strategy (1=yes)"
)
LLM_CONSECUTIVE_FAILURES = Gauge(
    "hedgehog_llm_consecutive_failures",
    "Consecutive LLM API failures"
)
UNHEDGED_POSITIONS = Gauge(
    "hedgehog_unhedged_positions",
    "Number of positions with failed rollbacks"
)
RATE_VALIDATIONS_REJECTED = Counter(
    "hedgehog_rate_validations_rejected_total",
    "Funding rates rejected by validation",
    ["venue", "reason"]
)
ROLLBACK_ATTEMPTS = Counter(
    "hedgehog_rollback_attempts_total",
    "Rollback attempts on partial fills",
    ["venue", "outcome"]
)
```

---

## Summary — Change Order

1. **Data validation** in `funding_collector.py` — stops bad data from entering the pipeline
2. **Fallback strategy** — new file + wiring in `main.py` — removes single point of failure
3. **Execution rollback hardening** — retries + unhedged tracking — prevents silent risk
4. **Circuit breaker expansion** — catches unhedged exposure from #3
5. **Wallet management consolidation** — single auth path, deprecate env var sprawl
6. **Risk engine config alignment** — read limits from YAML, add margin utilization check
7. **Package init files** — unblocks imports for new modules
8. **Test suite** — risk engine, circuit breaker, rollback — the critical safety path
9. **Config reconciliation** — standardize on YAML + Pydantic throughout
10. **Metrics** — observability for everything above