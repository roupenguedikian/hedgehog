---
name: drift
description: Query Drift Protocol (Solana) for funding rates, market prices, account balance, positions, open orders, recent fills, and funding payments. Use when the user asks about Drift positions, balances, funding rates, markets, or trading activity.
allowed-tools: Bash
argument-hint: "[markets|funding|account|positions|orders|fills|income|all] [address]"
---

# Drift Protocol Query

Query the Drift DEX (Solana) via the Data API for live market and account data.

Drift uses the Data API at `https://data.api.drift.trade` (no auth needed for reads). Symbols use `{symbol}-PERP` format (e.g. SOL-PERP). Funding cycle is 1 hour (8,760 payments/year). Wallet address is in `.env` (DRIFT_WALLET_ADDRESS).

## What to query

Based on `$ARGUMENTS`, run the appropriate section(s). Default is `all`.

- **`markets`** — Top 20 perpetual markets by volume with oracle price, funding rate, annualized %, OI, volume + extreme funding scanner
- **`funding`** — Average funding rates (24h/7d/30d/1y) + recent funding history (last 5 entries) for top 15 symbols
- **`account`** — Account balance, collateral, margin, health, asset balances, performance snapshot (PnL, fees, volume)
- **`positions`** — Open perpetual positions with side, size, entry price, mark, unrealized PnL, liquidation price
- **`orders`** — Open limit orders
- **`fills`** — Last 15 fills with timestamps, side, size, price, fees, maker/taker role
- **`income`** — Funding payment history grouped by market with net totals
- **`all`** or no argument — Run everything above

An optional Solana address (base58, ~44 chars) can be passed to override the default from `.env`.

## How to run

```bash
.venv/bin/python scripts/drift_query.py $ARGUMENTS
```

## Presentation

- Format results in clean markdown tables
- Highlight anything notable: large unrealized PnL, high funding rates (>10% annualized), unusual fills
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- For funding rates, always show both per-period and annualized
- Flag negative funding on major coins (potential long opportunity for the arb strategy)
- If repeated in the same session, note changes from previous query
