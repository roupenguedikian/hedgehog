Architecture Overview

  ┌──────────────────────────────────────────────────┐
  │              BotEngine (main.py)                 │
  ├──────────────────────────────────────────────────┤
  │  1. DATA        FundingRateCollector (30s poll)   │
  │                 CoinGlass + DefiLlama supplements │
  │                                                   │
  │  2. ANALYSIS    VenueScorer (7 weighted factors)  │
  │                                                   │
  │  3. DECISION    Claude LLM Agents                 │
  │                 ├─ Strategist → proposes trades   │
  │                 └─ Risk → APPROVE/RESIZE/REJECT   │
  │                                                   │
  │  4. EXECUTION   ExecutionEngine (atomic dual-leg) │
  │                 └─ Adapters (per-venue)            │
  │                                                   │
  │  5. SAFETY      CircuitBreaker (non-AI, always on)│
  │                 └─ Force-close on hard halt        │
  └──────────────────────────────────────────────────┘

  Key Design Decisions

  - Adapter pattern — BaseDefiAdapter abstraction for 9 DEXs across 8 chains. Only Hyperliquid is fully
  implemented; 5 venues remain stubs via GenericRestAdapter.
  - LLM-driven strategy with non-AI safety — Claude agents propose/review trades, but the circuit breaker
  operates independently and cannot be overridden by agents. Good separation of concerns.
  - Three operating modes — OBSERVE_ONLY (default/safe), SEMI_AUTO (human approval for large trades),
  FULL_AUTO.
  - Quarter-Kelly position sizing — conservative approach capped at 20% NAV per position.
  - YAML config (3 files) — venues, strategy, risk. Risk limits are read-only at runtime.

  Stack

  ┌────────────┬────────────────────────────────────┐
  │   Layer    │             Technology             │
  ├────────────┼────────────────────────────────────┤
  │ Language   │ Python 3.12, async (aiohttp/httpx) │
  ├────────────┼────────────────────────────────────┤
  │ AI         │ Anthropic Claude API               │
  ├────────────┼────────────────────────────────────┤
  │ Database   │ TimescaleDB + Redis                │
  ├────────────┼────────────────────────────────────┤
  │ Monitoring │ Prometheus + Grafana               │
  ├────────────┼────────────────────────────────────┤
  │ Deploy     │ Docker Compose (5 services)        │
  ├────────────┼────────────────────────────────────┤
  │ Config     │ YAML + .env                        │
  └────────────┴────────────────────────────────────┘

  Strengths

  1. Layered safety — 7 hard risk limits + independent circuit breaker that force-closes all positions on
  drawdown/loss thresholds
  2. Clean adapter abstraction — venue-agnostic core logic, easy to add new DEXs
  3. Funding rate normalization — all venues converted to annualized % for fair comparison
  4. Atomic dual-leg execution — parallel order placement with rollback on partial fills
  5. Multi-chain wallet management — derives EVM/Solana/Cosmos/StarkNet keys from a single mnemonic

  Concerns

  1. No test suite — tests/ is empty. Critical for a system handling real money.
  2. 8 of 9 adapters incomplete — only Hyperliquid is fully implemented. The GenericRestAdapter subclasses
  are stubs.
  3. Dual config systems — older modules import from config.venues while newer code uses YAML + Pydantic.
  Technical debt that could cause inconsistencies.
  4. No CI/CD pipeline — no GitHub Actions or equivalent.
  5. Cross-chain atomicity gap — dual-leg trades across different chains have no atomic guarantee; relies on
   a 2-second timing window.
  6. LLM dependency for core decisions — API latency/outages directly impact the trading loop. No fallback
  heuristic strategy.

  Recommendation Priority

  1. Tests first — unit tests for risk engine, circuit breaker, and execution engine rollback logic
  2. Reconcile config systems — standardize on YAML + Pydantic throughout
  3. Add a fallback non-LLM strategy — simple threshold-based logic when Claude API is unavailable
  4. Complete at least 2-3 more adapters — diversification across venues is core to the strategy
  5. CI/CD — automated linting, type checking, and test runs before deploy