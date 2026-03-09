# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Hedgehog** (codename "Aegis Protocol") is a DeFi-native perpetual funding rate arbitrage bot. It shorts high-funding-rate venues and longs low-funding-rate venues to capture the spread. All 9 venues are non-custodial DEXs — no centralized exchanges.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run modes
python main.py                        # OBSERVE_ONLY (default, safe — logs what it would do)
python main.py --mode SEMI_AUTO       # Human approval for trades above threshold
python main.py --mode FULL_AUTO       # Fully autonomous trading
python main.py --score-only           # Score venues and print scoreboard, then exit
python main.py --collect-only         # Collect funding rates continuously

# Docker (includes Redis, TimescaleDB, Grafana, Prometheus)
docker-compose up -d

# Database init
# Apply scripts/init_db.sql to TimescaleDB (postgres://aegis:aegis_dev@localhost:5432/aegis)
```

No test suite exists yet — `tests/` contains only `__init__.py`.

## Architecture

### Data Flow

1. **FundingRateCollector** (`services/data/funding_collector.py`) polls all connected venue adapters every 30s, supplemented by CoinGlass and DefiLlama APIs
2. **VenueScorer** (`services/data/venue_scorer.py`) computes composite scores (7 weighted factors: rate, consistency, liquidity, fees, cycle frequency, maturity, uptime)
3. **BotEngine** (inline in `main.py`) runs the strategy loop: collect → score → find opportunities → risk-check → execute/log
4. **RiskEngine** (`services/risk/risk_engine.py`) evaluates trades against portfolio limits; **CircuitBreaker** monitors continuously and force-closes all positions on halt
5. **ExecutionEngine** (`services/capital/execution_engine.py`) handles atomic dual-leg entry/exit/rotation with rollback on partial fills

### Adapter Pattern

All venue integrations extend `BaseDefiAdapter` (`adapters/base_adapter.py`), which defines the interface: `connect`, `get_funding_rate`, `get_orderbook`, `place_limit_order`, `place_market_order`, `get_positions`, `get_balance`, etc.

- **HyperliquidAdapter** — fully implemented, uses `httpx` for data + official SDK for trading
- **GenericRestAdapter** — base for REST venues; subclassed by `AsterAdapter`, `LighterAdapter`, `EtherealAdapter`, `ApexAdapter` with endpoint overrides. Trading methods are stubs that need per-venue signing logic.
- Several adapters (Drift, dYdX, Paradex, Injective) fall back to `GenericRestAdapter` with TODOs for full implementations

### Agent Layer

The bot uses an agentic architecture with LLM-driven strategy:
- **Strategist prompt** and **Risk prompt** defined in `agents/orchestrator.py`
- **Funding tools** (`agents/tools/funding_tools.py`) expose market data functions the agent can call: rate matrix, top opportunities, venue scores, net yield estimation
- Uses Anthropic Claude API (`anthropic` SDK in requirements)

### Key Models

All in `models/core.py` as Pydantic models:
- `FundingRate` — raw rate per cycle with `annualized` computed field (rate * 8760/cycle_hours)
- `VenuePairOpportunity` — arbitrage opportunity between two venues
- `HedgePosition` — paired short+long `Position` legs
- `TradeAction` / `AgentMessage` — agent communication
- `VenueConfig` — per-venue configuration (chain type, fees, funding cycle, DeFi-specific flags)

### Configuration

YAML files in `config/`:
- `venues.yaml` — venue profiles, symbol list, symbol format/overrides, data source config
- `strategy.yaml` — min spread (10% annual), position sizing (quarter-Kelly), execution limits, scoring weights, loop timing, operating mode
- `risk.yaml` — hard limits (5% drawdown, 25% per venue, 40% per chain, 60% margin util), circuit breaker triggers, tier-based allocation caps

### Environment Variables

- `EVM_PRIVATE_KEY` — fallback wallet key for all EVM venues
- `{VENUE}_PRIVATE_KEY` — venue-specific override (e.g., `HYPERLIQUID_PRIVATE_KEY`)
- `COINGLASS_API_KEY` — for aggregated funding rate data
- `DB_PASSWORD`, `GRAFANA_PASSWORD` — for Docker services

### Important Design Notes

- **Two versions of some modules exist**: `services/risk/risk_engine.py` and `services/data/venue_scorer.py` import from `models` and `config.venues` (old-style), while `main.py` and other modules use `models.core` and YAML config. These are not fully reconciled.
- **WalletManager** (`services/wallet/wallet_manager.py`) derives chain-specific keys (EVM, Solana, Cosmos, StarkNet) from a single mnemonic, but `main.py` currently reads keys from env vars directly.
- Funding rates are normalized to annualized via `rate * (8760 / cycle_hours)`. Venues use 1h or 8h cycles.
- The `symbol_format` field in venue config handles symbol normalization (e.g., `{symbol}` for Hyperliquid, `{symbol}-USD-PERP` for Paradex).
- `services/bridge/` and `services/monitoring/` are empty placeholder packages.
