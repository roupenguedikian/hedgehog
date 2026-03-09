# Hedgehog — Implementation Task List

Concrete changes derived from the architecture review. Each task includes the problem, the fix, files to modify, and step-by-step coding instructions referencing actual line numbers and interfaces.

---

## Task 1: Add Timeout to Dual-Leg Execution

**Priority: CRITICAL**
**Files:** `services/capital/execution_engine.py`

**Problem:** `enter_hedge()` (line 113) and `exit_hedge()` (line 268) both call `asyncio.gather(short_task, long_task, return_exceptions=True)` with no timeout. If one venue's API hangs, the bot blocks indefinitely. The constant `ORDER_TIMEOUT_SECONDS = 30` exists (line 28) but is never used.

**Instructions:**

1. In `enter_hedge()`, wrap the gather at line 113:
   ```python
   # Replace line 113:
   #   results = await asyncio.gather(short_task, long_task, return_exceptions=True)
   # With:
   try:
       results = await asyncio.wait_for(
           asyncio.gather(short_task, long_task, return_exceptions=True),
           timeout=ORDER_TIMEOUT_SECONDS,
       )
   except asyncio.TimeoutError:
       logger.error("execution.dual_leg_timeout",
                    symbol=symbol, short=short_venue, long=long_venue)
       m.ORDER_ERRORS_TOTAL.labels(venue=short_venue, error_type="timeout").inc()
       m.ORDER_ERRORS_TOTAL.labels(venue=long_venue, error_type="timeout").inc()
       # Best-effort cancel any pending orders
       for adapter in [short_adapter, long_adapter]:
           try:
               await adapter.cancel_all_orders()
           except Exception:
               pass
       return {"status": "TIMEOUT", "reason": "Dual-leg execution timed out"}
   ```

2. Apply the same pattern to `exit_hedge()` at line 268. On timeout, log and return `{"status": "TIMEOUT"}` — do NOT delete the position from `active_positions` since it's still open.

3. In `_rebalance_legs()` (line 375), wrap the market order call in `asyncio.wait_for` with `ORDER_TIMEOUT_SECONDS` as well.

---

## Task 2: Harden Rollback with Retries + Unhedged Tracking

**Priority: CRITICAL**
**Files:** `services/capital/execution_engine.py`

**Problem:** `_rollback()` (lines 357-373) has a single try/except that catches all exceptions and only logs. If the rollback market order fails, the bot has an unhedged directional position with no signal to the circuit breaker or operator.

**Instructions:**

1. Add `_unhedged_positions` to `__init__` (line 40-42):
   ```python
   def __init__(self, adapters: dict):
       self.adapters = adapters
       self.active_positions: dict[str, HedgePosition] = {}
       self._unhedged_positions: list[dict] = []
   ```

2. Add a property after `__init__`:
   ```python
   @property
   def has_unhedged_exposure(self) -> bool:
       return len(self._unhedged_positions) > 0
   ```

3. Replace `_rollback()` (lines 357-373) with a retry loop:
   ```python
   async def _rollback(self, short_result, long_result,
                       short_adapter, long_adapter,
                       short_symbol, long_symbol):
       """Rollback filled legs with retries. Track failures for circuit breaker."""
       MAX_RETRIES = 3

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
                       short_result = None
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
                       long_result = None
           except Exception as e:
               logger.error("execution.rollback_long_failed",
                            attempt=attempt, error=str(e))

           # Check if both sides are clean
           short_remaining = (short_result and isinstance(short_result, OrderResult)
                              and short_result.filled_qty > 0)
           long_remaining = (long_result and isinstance(long_result, OrderResult)
                             and long_result.filled_qty > 0)

           if not short_remaining and not long_remaining:
               logger.info("execution.rollback_complete", attempts=attempt + 1)
               return

           await asyncio.sleep(0.5 * (attempt + 1))

       # Exhausted retries — track unhedged exposure
       logger.critical("execution.ROLLBACK_FAILED_UNHEDGED",
                       short_remaining=bool(short_result),
                       long_remaining=bool(long_result))
       self._unhedged_positions.append({
           "short": short_result,
           "long": long_result,
           "short_symbol": short_symbol,
           "long_symbol": long_symbol,
           "timestamp": datetime.now(timezone.utc).isoformat(),
       })
   ```

4. Add `from datetime import datetime, timezone` to the imports at the top of the file (it's not currently imported).

---

## Task 3: Circuit Breaker — Detect Unhedged Exposure

**Priority: HIGH**
**Files:** `services/risk/circuit_breaker.py`, `main.py`

**Problem:** The circuit breaker (lines 27-44) only checks drawdown and absolute loss. It doesn't know about unhedged positions from failed rollbacks (Task 2). Also, `config/risk.yaml` defines `liquidation_cascade_count` and `exchange_down_sec` triggers that are not implemented.

**Instructions:**

1. Add an `execution_engine` reference to `CircuitBreaker.__init__` (line 16):
   ```python
   def __init__(self, config: dict, adapters: dict, alerter=None):
       self.max_loss_usd = config.get("max_loss_usd", 10000)
       self.max_drawdown_pct = config.get("max_drawdown_pct", 7.0)
       self.adapters = adapters
       self.alerter = alerter
       self.triggered = False
       self.initial_nav: float = 0
       self.execution_engine = None  # set after construction
   ```

2. Add the unhedged exposure check to `check()`, after the drawdown check (after line 42):
   ```python
   # Unhedged exposure check
   if (self.execution_engine
           and self.execution_engine.has_unhedged_exposure):
       await self._trigger(
           f"Unhedged exposure: "
           f"{len(self.execution_engine._unhedged_positions)} failed rollbacks"
       )
       return True
   ```

3. In `main.py`, wire the execution engine to the circuit breaker. After the `ExecutionEngine` is created (it's not currently instantiated — needs to be added), set:
   ```python
   # After creating execution_engine and circuit_breaker in run_full_bot():
   from services.capital.execution_engine import ExecutionEngine
   execution_engine = ExecutionEngine(connected)
   circuit_breaker.execution_engine = execution_engine
   ```
   Note: `run_full_bot()` currently does NOT create an ExecutionEngine. It imports RiskEngine and CircuitBreaker (lines 241-242) but not ExecutionEngine. Add the import and instantiation after line 268.

---

## Task 4: Funding Rate Data Validation

**Priority: HIGH**
**Files:** `services/data/funding_collector.py`, `models/core.py`

**Problem:** `collect_once()` (lines 57-116) stores every rate returned by adapters with no sanity checks. A stale, nonsensical, or divergent rate feeds directly into the scorer and strategy layer. Three data sources (adapters, CoinGlass, DefiLlama) could disagree with no cross-validation.

**Instructions:**

1. Add a `metadata` field to `FundingRate` in `models/core.py` (after line 67):
   ```python
   metadata: dict = Field(default_factory=dict)  # flags like "divergence_flag"
   ```

2. Add validation constants and method to `FundingRateCollector` class in `funding_collector.py`. After line 55 (`self._max_history = 10000`), add:
   ```python
   # Validation bounds
   RATE_BOUNDS_MIN_ANNUAL = -5.0   # -500% annual is absurd
   RATE_BOUNDS_MAX_ANNUAL = 5.0    # +500% annual is absurd
   MAX_CROSS_SOURCE_DIVERGENCE = 0.10  # 10% annualized

   def _validate_rate(self, rate: FundingRate) -> bool:
       """Reject rates that are stale, out of bounds, or flag divergent ones."""
       # 1. Staleness
       age = (datetime.now(timezone.utc) - rate.timestamp).total_seconds()
       if age > 300:  # matches risk.yaml stale_data_threshold_sec
           logger.warning("funding.stale_rate",
                          venue=rate.venue, symbol=rate.symbol, age_sec=age)
           return False

       # 2. Bounds
       if not (self.RATE_BOUNDS_MIN_ANNUAL
               <= rate.annualized
               <= self.RATE_BOUNDS_MAX_ANNUAL):
           logger.warning("funding.rate_out_of_bounds",
                          venue=rate.venue, symbol=rate.symbol,
                          annualized=rate.annualized)
           return False

       # 3. Cross-source divergence (flag, don't reject)
       other_rates = [
           r.annualized for (v, s), r in self.latest_rates.items()
           if s == rate.symbol and v != rate.venue
       ]
       if other_rates:
           median = sorted(other_rates)[len(other_rates) // 2]
           divergence = abs(rate.annualized - median)
           if divergence > self.MAX_CROSS_SOURCE_DIVERGENCE:
               logger.warning("funding.rate_divergence",
                              venue=rate.venue, symbol=rate.symbol,
                              rate=rate.annualized, median=median)
               rate.metadata["divergence_flag"] = True

       return True
   ```

3. Gate rate insertion in `collect_once()`. Replace lines 82-85:
   ```python
   # Replace:
   #   key = (result.venue, result.symbol)
   #   self.latest_rates[key] = result
   #   self.rate_history.append(result)
   #   successful += 1
   # With:
   if self._validate_rate(result):
       key = (result.venue, result.symbol)
       self.latest_rates[key] = result
       self.rate_history.append(result)
       successful += 1
   else:
       logger.info("funding.rate_rejected",
                    venue=result.venue, symbol=result.symbol)
   ```

---

## Task 5: LLM Fallback Strategy

**Priority: HIGH**
**Files:** new `services/strategy/__init__.py`, new `services/strategy/fallback_strategy.py`, `main.py`

**Problem:** The Claude API is in the critical path of every trading cycle. If the API is down or slow, the bot stalls entirely (no fallback in `run_full_bot()`). An outage during a favorable funding window is direct opportunity cost.

**Instructions:**

1. Create `services/strategy/__init__.py` (empty or with docstring).

2. Create `services/strategy/fallback_strategy.py`. Key design constraints based on actual interfaces:
   - Opportunities come from `collector.latest_opportunities` (a `list[VenuePairOpportunity]`) — this DOES exist (line 49 of funding_collector.py, populated at line 100).
   - Venue scores are in `scorer._scores` (a `dict[tuple[str, str], VenueScore]`). The scorer does NOT have a `get_score(venue, symbol)` method — use `scorer._scores.get((venue, symbol))` or add a simple accessor.
   - `VenueTier` is a `str` Enum (line 35-38 of core.py): values are `"tier_1"`, `"tier_2"`, `"tier_3"`. Compare with `vc.tier.value` or `vc.tier == VenueTier.TIER_1`.
   - `VenueConfig.tier` is a `VenueTier` enum, accessed as `vc.tier`.

   ```python
   """
   Deterministic fallback strategy for when Claude API is unavailable.
   No LLM calls — pure math on collector + scorer outputs.
   """
   from __future__ import annotations
   import structlog
   from models.core import TradeAction, ActionType, VenueTier

   logger = structlog.get_logger()

   FALLBACK_SYMBOLS = {"BTC", "ETH"}
   FALLBACK_SIZE_PCT = 0.05     # 5% of NAV per position
   MIN_VENUE_SCORE = 0.4
   MAX_NEW_POSITIONS_PER_CYCLE = 2
   ALLOWED_TIERS = {VenueTier.TIER_1, VenueTier.TIER_2}


   class FallbackStrategy:
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
               if opp.symbol not in FALLBACK_SYMBOLS:
                   continue
               if opp.spread_annual < self.min_spread:
                   continue

               # Check venue tiers
               short_vc = self.venue_configs.get(opp.short_venue)
               long_vc = self.venue_configs.get(opp.long_venue)
               if not short_vc or not long_vc:
                   continue
               if short_vc.tier not in ALLOWED_TIERS:
                   continue
               if long_vc.tier not in ALLOWED_TIERS:
                   continue

               # Check venue scores via scorer._scores
               short_score = self.scorer._scores.get((opp.short_venue, opp.symbol))
               long_score = self.scorer._scores.get((opp.long_venue, opp.symbol))
               if not short_score or short_score.composite_score < MIN_VENUE_SCORE:
                   continue
               if not long_score or long_score.composite_score < MIN_VENUE_SCORE:
                   continue

               # Skip if we already have a position on either venue for this symbol
               already_positioned = False
               for pos in existing_positions.values():
                   if (pos.symbol == opp.symbol and
                       (pos.short_leg.venue == opp.short_venue or
                        pos.long_leg.venue == opp.long_venue)):
                       already_positioned = True
                       break
               if already_positioned:
                   continue

               size = portfolio_nav * FALLBACK_SIZE_PCT
               actions.append(TradeAction(
                   action_type=ActionType.ENTER_HEDGE,
                   symbol=opp.symbol,
                   short_venue=opp.short_venue,
                   long_venue=opp.long_venue,
                   size_usd=size,
                   expected_annual_yield=opp.spread_annual,
                   confidence=0.6,
                   reasoning=(f"[FALLBACK] spread={opp.spread_annual*100:.1f}% "
                              f"short={opp.short_venue} long={opp.long_venue}"),
               ))

               if len(actions) >= MAX_NEW_POSITIONS_PER_CYCLE:
                   break

           return actions
   ```

3. Wire into `main.py`'s `BotEngine`. In `run_full_bot()`, after creating `scorer` (line 258):
   ```python
   from services.strategy.fallback_strategy import FallbackStrategy
   fallback = FallbackStrategy(
       collector=collector,
       scorer=scorer,
       venue_configs=venue_configs,
       min_spread_annual=configs["strategy"].get("min_spread_annual", 0.10) + 0.02,
   )
   ```

4. In `BotEngine.__init__` (line 281), add:
   ```python
   self._llm_consecutive_failures = 0
   ```

5. The current `BotEngine.run_cycle()` doesn't call the LLM directly (it evaluates opportunities inline). When LLM orchestration is wired in, wrap that call:
   ```python
   # When the LLM call exists, wrap it with:
   try:
       actions = await asyncio.wait_for(
           self._llm_strategy_cycle(opportunities), timeout=30.0)
       self._llm_consecutive_failures = 0
   except (asyncio.TimeoutError, Exception) as e:
       self._llm_consecutive_failures += 1
       logger.warning("bot.llm_unavailable",
                      error=str(e), consecutive=self._llm_consecutive_failures)
       if self._llm_consecutive_failures >= 2:
           logger.info("bot.using_fallback_strategy")
           actions = fallback.generate_actions(
               portfolio_nav=self.portfolio.total_nav,
               existing_positions=execution_engine.active_positions,
           )
       else:
           actions = []
   ```
   For now (no LLM call in the loop yet), this wiring can be deferred until the orchestrator is integrated.

---

## Task 6: Risk Engine — Read Position Limit from Config + Add Margin Check

**Priority: HIGH**
**Files:** `services/risk/risk_engine.py`, `models/core.py`

**Problem:**
- `max_pos_pct` is hardcoded to `20.0` (line 43) instead of reading from the injected `self.config` dict. The config value exists in `strategy.yaml` as `max_single_position_pct: 0.20`.
- `risk.yaml` defines `max_margin_utilization: 0.60` but no margin utilization check exists in the risk engine.
- `min_yield` is hardcoded to `0.10` (line 107) instead of reading from `strategy.yaml`'s `min_spread_annual: 0.10`.
- `PortfolioSnapshot` has no `margin_used` field, so the margin check needs portfolio-level aggregation.

**Instructions:**

1. Add `margin_used` field to `PortfolioSnapshot` in `models/core.py` (after line 243):
   ```python
   margin_used: float = 0.0  # total margin in use across all venues
   ```

2. In `risk_engine.py`, replace the hardcoded position size limit at line 43:
   ```python
   # Replace:
   #   max_pos_pct = 20.0
   # With:
   max_pos_pct = self.config.get("max_single_position_pct", 0.20) * 100
   ```
   Note: `strategy.yaml` stores this as `0.20` (decimal), but the risk engine works in percentages. If risk config stores it differently, adjust. Check what config dict is passed — it's `configs["risk"]` from `main.py` line 261. The `max_single_position_pct` is in `strategy.yaml`, not `risk.yaml`. Either:
   - Pass strategy config too: `RiskEngine(config=configs["risk"], strategy_config=configs["strategy"], ...)`, or
   - Move `max_single_position_pct` to the risk config dict before passing, or
   - Add it to risk.yaml.

   Simplest: in `main.py`, merge relevant strategy keys into risk config before passing:
   ```python
   risk_config = configs["risk"]
   risk_config["max_single_position_pct"] = configs["strategy"].get("max_single_position_pct", 0.20)
   risk_config["min_spread_annual"] = configs["strategy"].get("min_spread_annual", 0.10)
   risk_engine = RiskEngine(config=risk_config, venue_configs=venue_configs)
   ```

3. Replace the hardcoded min_yield at line 107:
   ```python
   # Replace:
   #   min_yield = 0.10
   # With:
   min_yield = self.config.get("min_spread_annual", 0.10)
   ```

4. Add margin utilization check after the in-transit check (after line 127):
   ```python
   # 8. Margin utilization
   max_margin_util = self.config.get("max_margin_utilization", 0.60)
   if portfolio.margin_used > 0 and nav > 0:
       current_util = portfolio.margin_used / nav
       # Estimate additional margin from this trade (assume 10x leverage)
       projected_util = current_util + (action.size_usd * 0.1 / nav)
       if projected_util > max_margin_util:
           checks.append(RiskCheck(
               name="margin_utilization",
               status="WARNING",
               value=projected_util * 100,
               limit=max_margin_util * 100,
               message=f"Margin util {projected_util*100:.1f}% > {max_margin_util*100:.0f}% limit",
           ))
   ```

5. Populate `margin_used` in `BotEngine.run_cycle()` in `main.py`. Currently the portfolio is initialized with `PortfolioSnapshot(total_nav=100000)` (line 282). When updating portfolio state, sum margin from venue balances:
   ```python
   # After fetching venue balances in run_cycle, update portfolio:
   total_margin = sum(
       balance.get("margin", balance.get("margin_used", 0))
       for balance in venue_balance_data.values()
   )
   self.portfolio.margin_used = total_margin
   ```

---

## Task 7: Wire ExecutionEngine into BotEngine

**Priority: HIGH**
**Files:** `main.py`

**Problem:** `run_full_bot()` creates RiskEngine and CircuitBreaker but never creates an ExecutionEngine. The `BotEngine.run_cycle()` evaluates trades and logs them but never executes, even in `FULL_AUTO` mode (line 472-474 is a comment `# Execute here...`).

**Instructions:**

1. Add import and instantiation in `run_full_bot()`, after line 242:
   ```python
   from services.capital.execution_engine import ExecutionEngine
   ```

2. After `circuit_breaker` creation (line 268), add:
   ```python
   execution_engine = ExecutionEngine(connected)
   circuit_breaker.execution_engine = execution_engine
   ```

3. Replace the placeholder at lines 472-474:
   ```python
   # Replace:
   #   elif mode == "FULL_AUTO":
   #       logger.info("bot.executing", action=action.model_dump())
   #       # Execute here...
   # With:
   elif mode == "FULL_AUTO":
       logger.info("bot.executing", action=action.model_dump())
       exec_result = await execution_engine.execute(action)
       logger.info("bot.executed", result=exec_result)
   ```

4. Similarly for `SEMI_AUTO` mode, add execution after approval (line 476 is currently just a log).

5. Add position update calls in the cycle. Before opportunity detection (before line 436), add:
   ```python
   await execution_engine.update_positions()
   ```

---

## Task 8: Consolidate Wallet Management

**Priority: MEDIUM**
**Files:** `main.py`, `adapters/base_adapter.py`

**Problem:** `main.py` line 117 reads private keys from env vars (`os.getenv(f"{name.upper()}_PRIVATE_KEY")`) and passes raw strings to `adapter.connect(pk)`. The `WalletManager` in `services/wallet/wallet_manager.py` exists with full EVM/Solana/Cosmos/StarkNet derivation from a mnemonic but is never used.

**Instructions:**

1. Add `set_wallet` method to `BaseDefiAdapter` in `adapters/base_adapter.py`:
   ```python
   def set_wallet(self, wallet):
       """Set wallet credentials. Adapters can override for chain-specific setup."""
       self._wallet = wallet
       self.private_key = wallet.private_key
       self.address = wallet.address
   ```

2. In `main.py`, update `connect_adapters()` (lines 112-138):
   ```python
   async def connect_adapters(adapters: dict, venue_configs: dict) -> dict:
       """Connect all adapters. Prefer WalletManager, fall back to env vars."""
       from services.wallet.wallet_manager import WalletManager

       mnemonic = os.getenv("MASTER_MNEMONIC", "")
       wallet_mgr = None
       if mnemonic:
           wallet_mgr = WalletManager(mnemonic=mnemonic)

       connected = {}
       for name, adapter in adapters.items():
           # Try WalletManager first, then env var fallback
           pk = ""
           if wallet_mgr:
               try:
                   wallet = wallet_mgr.get_wallet_for_venue(name)
                   adapter.set_wallet(wallet)
                   pk = wallet.private_key
               except Exception as e:
                   logger.debug(f"WalletManager skip for {name}: {e}")

           if not pk:
               pk = os.getenv(f"{name.upper()}_PRIVATE_KEY",
                              os.getenv("EVM_PRIVATE_KEY", ""))

           if not pk:
               logger.info(f"No private key for {name} — running in read-only mode")
               try:
                   await adapter.connect("")
                   connected[name] = adapter
               except Exception:
                   pass
               continue

           try:
               success = await adapter.connect(pk)
               if success:
                   connected[name] = adapter
                   logger.info(f"Connected: {name}")
               else:
                   logger.warning(f"Failed to connect: {name}")
           except Exception as e:
               logger.warning(f"Connection error for {name}: {e}")

       return connected
   ```

---

## Task 9: Add Metrics for New Safety Features

**Priority: MEDIUM**
**Files:** `services/monitoring/metrics.py`

**Problem:** The metrics file already has comprehensive Prometheus gauges (303 lines), but lacks metrics for the new features from Tasks 1-5: LLM fallback state, unhedged positions, rate validation rejections, and rollback retry outcomes.

**Instructions:**

Add the following after the `ROLLBACKS_TOTAL` counter (after line 279):

```python
# ── New safety metrics ──────────────────────────────────────────────────

LLM_FALLBACK_ACTIVE = Gauge(
    "hedgehog_llm_fallback_active",
    "Whether the bot is using fallback strategy (1=yes)",
)
LLM_CONSECUTIVE_FAILURES = Gauge(
    "hedgehog_llm_consecutive_failures",
    "Consecutive LLM API failures",
)
UNHEDGED_POSITIONS = Gauge(
    "hedgehog_unhedged_positions",
    "Number of positions with failed rollbacks",
)
RATE_VALIDATIONS_REJECTED = Counter(
    "hedgehog_rate_validations_rejected_total",
    "Funding rates rejected by validation",
    ["venue", "reason"],
)
ROLLBACK_ATTEMPTS = Counter(
    "hedgehog_rollback_attempts_total",
    "Rollback attempts on partial fills",
    ["venue", "outcome"],
)
```

Then instrument the code in the relevant modules:
- In `execution_engine.py` `_rollback()`: increment `ROLLBACK_ATTEMPTS` on each attempt with outcome label ("success" / "failed").
- In `execution_engine.py`: set `UNHEDGED_POSITIONS` gauge after appending to `_unhedged_positions`.
- In `funding_collector.py` `_validate_rate()`: increment `RATE_VALIDATIONS_REJECTED` with venue and reason labels ("stale", "out_of_bounds").
- In `main.py` `BotEngine`: set `LLM_FALLBACK_ACTIVE` and `LLM_CONSECUTIVE_FAILURES` during the cycle.

---

## Task 10: Test Suite — Critical Safety Path

**Priority: HIGH**
**Files:** new `tests/conftest.py`, new `tests/test_risk_engine.py`, new `tests/test_circuit_breaker.py`, new `tests/test_execution_rollback.py`, new `tests/test_funding_validation.py`, `pyproject.toml` or `pytest.ini`

**Problem:** `tests/` contains only `__init__.py`. No automated tests exist for a system handling real money.

**Instructions:**

1. Add pytest config. Create or append to `pyproject.toml`:
   ```toml
   [tool.pytest.ini_options]
   asyncio_mode = "auto"
   testpaths = ["tests"]
   ```

2. Add `pytest` and `pytest-asyncio` to `requirements.txt` if not present.

3. **`tests/conftest.py`** — Shared fixtures. Key gotchas:
   - `PortfolioSnapshot.drawdown_pct` is a `@computed_field` — it is derived from `peak_nav` and `total_nav`. You CANNOT pass it as a constructor argument. To get a portfolio with 6% drawdown, set `peak_nav=100000, total_nav=94000`.
   - `VenueConfig` has many fields. Use a helper fixture that provides all required fields.
   - `OrderResult` requires `venue`, `symbol`, `side` fields (not just `status` and `filled_qty`).

   ```python
   import pytest
   from models.core import (
       PortfolioSnapshot, VenueConfig, ChainType, VenueTier,
       OrderResult, OrderStatus, Side,
   )

   @pytest.fixture
   def venue_configs():
       base = dict(
           chain="arbitrum", chain_type=ChainType.EVM,
           funding_cycle_hours=1, maker_fee_bps=0.5, taker_fee_bps=1.0,
           max_leverage=20, api_base_url="https://example.com",
       )
       return {
           "hyperliquid": VenueConfig(
               name="hyperliquid", tier=VenueTier.TIER_1, **base),
           "drift": VenueConfig(
               name="drift", chain="solana", chain_type=ChainType.SOLANA,
               tier=VenueTier.TIER_1, funding_cycle_hours=1,
               maker_fee_bps=1.0, taker_fee_bps=2.0, max_leverage=20,
               api_base_url="https://example.com"),
       }

   @pytest.fixture
   def healthy_portfolio():
       return PortfolioSnapshot(
           total_nav=100000, peak_nav=100000)

   @pytest.fixture
   def drawdown_portfolio():
       """Portfolio with 6% drawdown (peak 100k, current 94k)."""
       return PortfolioSnapshot(
           total_nav=94000, peak_nav=100000)

   def make_order_result(status=OrderStatus.FILLED, filled_qty=1.0,
                         avg_price=50000, fee=0.5, venue="test",
                         symbol="BTC", side=Side.LONG):
       return OrderResult(
           venue=venue, symbol=symbol, side=side,
           status=status, filled_qty=filled_qty,
           avg_price=avg_price, fee=fee)
   ```

4. **`tests/test_risk_engine.py`**:
   ```python
   from models.core import TradeAction, ActionType, RiskDecision
   from services.risk.risk_engine import RiskEngine

   class TestRiskEngine:
       def test_halts_on_drawdown(self, venue_configs, drawdown_portfolio):
           config = {"max_drawdown_pct": 5.0}
           engine = RiskEngine(config, venue_configs)
           action = TradeAction(
               action_type=ActionType.ENTER_HEDGE, symbol="BTC",
               short_venue="hyperliquid", long_venue="drift",
               size_usd=10000, expected_annual_yield=0.15, confidence=0.8)
           decision, checks = engine.evaluate_trade(action, drawdown_portfolio)
           assert decision == RiskDecision.HALT

       def test_approves_valid_trade(self, venue_configs, healthy_portfolio):
           config = {"max_drawdown_pct": 5.0}
           engine = RiskEngine(config, venue_configs)
           action = TradeAction(
               action_type=ActionType.ENTER_HEDGE, symbol="BTC",
               short_venue="hyperliquid", long_venue="drift",
               size_usd=10000, expected_annual_yield=0.20, confidence=0.8)
           decision, checks = engine.evaluate_trade(action, healthy_portfolio)
           assert decision == RiskDecision.APPROVE

       def test_resizes_oversized_position(self, venue_configs, healthy_portfolio):
           config = {"max_drawdown_pct": 5.0, "max_single_position_pct": 0.20}
           engine = RiskEngine(config, venue_configs)
           action = TradeAction(
               action_type=ActionType.ENTER_HEDGE, symbol="BTC",
               short_venue="hyperliquid", long_venue="drift",
               size_usd=30000, expected_annual_yield=0.20, confidence=0.8)
           decision, checks = engine.evaluate_trade(action, healthy_portfolio)
           assert decision == RiskDecision.RESIZE
   ```

5. **`tests/test_circuit_breaker.py`**:
   ```python
   import pytest
   from unittest.mock import AsyncMock
   from models.core import PortfolioSnapshot
   from services.risk.circuit_breaker import CircuitBreaker

   @pytest.fixture
   def cb():
       config = {"max_loss_usd": 5000, "max_drawdown_pct": 5.0}
       adapters = {"hyperliquid": AsyncMock()}
       return CircuitBreaker(config, adapters)

   class TestCircuitBreaker:
       async def test_triggers_on_drawdown(self, cb):
           cb.set_initial_nav(100000)
           portfolio = PortfolioSnapshot(total_nav=94000, peak_nav=100000)
           triggered = await cb.check(portfolio)
           assert triggered is True
           assert cb.triggered is True

       async def test_triggers_on_absolute_loss(self, cb):
           cb.set_initial_nav(100000)
           # 3% drawdown (below 5% threshold) but $6000 loss (above $5000)
           portfolio = PortfolioSnapshot(total_nav=94000, peak_nav=97000)
           triggered = await cb.check(portfolio)
           assert triggered is True

       async def test_no_trigger_within_limits(self, cb):
           cb.set_initial_nav(100000)
           portfolio = PortfolioSnapshot(total_nav=97000, peak_nav=98000)
           triggered = await cb.check(portfolio)
           assert triggered is False

       async def test_stays_triggered_once_tripped(self, cb):
           cb.triggered = True
           portfolio = PortfolioSnapshot(total_nav=100000, peak_nav=100000)
           triggered = await cb.check(portfolio)
           assert triggered is True
   ```

6. **`tests/test_execution_rollback.py`**:
   ```python
   import pytest
   from unittest.mock import AsyncMock
   from models.core import OrderResult, OrderStatus, Side
   from services.capital.execution_engine import ExecutionEngine
   from tests.conftest import make_order_result

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
       async def test_rollback_closes_filled_short(self, mock_adapters):
           engine = ExecutionEngine(mock_adapters)
           short_fill = make_order_result(
               venue="venue_a", symbol="BTC", side=Side.SHORT)
           mock_adapters["venue_a"].place_market_order.return_value = (
               make_order_result(venue="venue_a", symbol="BTC", side=Side.LONG))
           await engine._rollback(
               short_fill, None,
               mock_adapters["venue_a"], mock_adapters["venue_b"],
               "BTC", "BTC")
           mock_adapters["venue_a"].place_market_order.assert_called()
           call_args = mock_adapters["venue_a"].place_market_order.call_args
           assert call_args[0][1] == Side.LONG

       async def test_both_none_no_rollback(self, mock_adapters):
           engine = ExecutionEngine(mock_adapters)
           await engine._rollback(
               None, None,
               mock_adapters["venue_a"], mock_adapters["venue_b"],
               "BTC", "BTC")
           mock_adapters["venue_a"].place_market_order.assert_not_called()
           mock_adapters["venue_b"].place_market_order.assert_not_called()

       async def test_failed_rollback_tracks_unhedged(self, mock_adapters):
           """After Task 2 is implemented: verify failed rollback is tracked."""
           engine = ExecutionEngine(mock_adapters)
           short_fill = make_order_result(
               venue="venue_a", symbol="BTC", side=Side.SHORT)
           mock_adapters["venue_a"].place_market_order.side_effect = Exception("RPC down")
           await engine._rollback(
               short_fill, None,
               mock_adapters["venue_a"], mock_adapters["venue_b"],
               "BTC", "BTC")
           # After Task 2: assert engine.has_unhedged_exposure
   ```

7. **`tests/test_funding_validation.py`** (after Task 4):
   ```python
   import pytest
   from datetime import datetime, timezone, timedelta
   from unittest.mock import AsyncMock
   from models.core import FundingRate
   from services.data.funding_collector import FundingRateCollector

   @pytest.fixture
   def collector():
       return FundingRateCollector(
           adapters={}, symbols=["BTC", "ETH"], min_spread_annual=0.10)

   class TestFundingValidation:
       def test_rejects_stale_rate(self, collector):
           rate = FundingRate(
               venue="test", symbol="BTC", rate=0.0001, cycle_hours=1,
               timestamp=datetime.now(timezone.utc) - timedelta(seconds=400))
           assert collector._validate_rate(rate) is False

       def test_rejects_absurd_rate(self, collector):
           rate = FundingRate(
               venue="test", symbol="BTC", rate=10.0, cycle_hours=1)
           # annualized = 10.0 * 8760 = 87600, way above 5.0
           assert collector._validate_rate(rate) is False

       def test_accepts_normal_rate(self, collector):
           rate = FundingRate(
               venue="test", symbol="BTC", rate=0.0001, cycle_hours=1)
           assert collector._validate_rate(rate) is True

       def test_flags_divergent_rate(self, collector):
           # Pre-populate with a baseline rate
           baseline = FundingRate(
               venue="venue_a", symbol="BTC", rate=0.0001, cycle_hours=1)
           collector.latest_rates[("venue_a", "BTC")] = baseline
           # Now validate a rate that diverges significantly
           divergent = FundingRate(
               venue="venue_b", symbol="BTC", rate=0.01, cycle_hours=1)
           result = collector._validate_rate(divergent)
           assert result is True  # accepted but flagged
           assert divergent.metadata.get("divergence_flag") is True
   ```

---

## Task 11: Update Architecture Overview Documents

**Priority: LOW**
**Files:** `Architecture Overview.md`, `ArchitectureOverview2.md`, `Architectoverview3.md`

**Problem:** The review found factual errors in all three documents.

**Instructions:**

1. **Architecture Overview.md:**
   - Line 24: Change "5 venues remain stubs" to "8 of 9 venues are incomplete (4 GenericRestAdapter subclasses with stub trading methods + 4 raw GenericRestAdapter instances with TODO comments)"
   - Line 65-66: Update Concern #3 — both risk_engine.py and venue_scorer.py already import from `models.core`, not `config.venues`. The real issue is hardcoded values (e.g., `max_pos_pct = 20.0`) not reading from config.
   - Add mention of `services/monitoring/metrics.py` (not empty — has full Prometheus integration)
   - Add mention of agent tools in `agents/tools/funding_tools.py`

2. **Architectoverview3.md:** Add a disclaimer at the top:
   ```
   > **Note:** Code samples below are design-level pseudocode.
   > They reference simplified interfaces and must be adapted to match
   > the actual class APIs before implementation. See TASKS.md for
   > corrected, implementation-ready instructions.
   ```

---

## Dependency Order

```
Task 4  (rate validation)      — standalone, no deps
Task 1  (execution timeout)    — standalone, no deps
Task 2  (rollback hardening)   — standalone, no deps
Task 3  (circuit breaker)      — depends on Task 2 (has_unhedged_exposure)
Task 6  (risk engine config)   — standalone, no deps
Task 7  (wire ExecutionEngine) — depends on Tasks 1, 2, 3
Task 5  (fallback strategy)    — standalone, but wiring depends on Task 7
Task 8  (wallet consolidation) — standalone, no deps
Task 9  (metrics)              — depends on Tasks 1-5 (instruments new code)
Task 10 (tests)                — can start immediately, some tests validate Tasks 1-6
Task 11 (docs)                 — do last
```

**Suggested implementation order:** 4 → 1 → 2 → 6 → 3 → 7 → 5 → 8 → 10 → 9 → 11
