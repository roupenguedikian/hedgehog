---
name: paradex
description: Query Paradex DEX (StarkNet) for funding rates, account balance, margin, positions, open orders, trade fills, and funding payments. Use when the user asks about Paradex positions, balances, funding rates, or trading activity.
allowed-tools: Bash
argument-hint: "[funding|account|positions|orders|fills|income|all]"
---

# Paradex DEX Query

Query the Paradex API for live market and account data.

Paradex runs on StarkNet with Ethereum settlement. API base is `https://api.prod.paradex.trade/v1`. Symbols use `{symbol}-USD-PERP` format (e.g., BTC-USD-PERP). Funding cycle is 8 hours (1,095 payments/year). Public data (funding) needs no auth. Account data requires PARADEX_L2_ADDRESS and PARADEX_L2_PRIVATE_KEY in `.env`, plus the `paradex-py` SDK for StarkNet signing.

## What to query

Based on `$ARGUMENTS`, run the appropriate section(s). Default is `all`.

- **`funding`** — Top 20 funding rates by 24h volume + extreme funding scanner (public, no auth)
- **`account`** — NAV (equity), total/free collateral, margin used/free, init/maint margin requirements, token balances
- **`positions`** — Open positions with entry, uPnL, unrealized funding PnL, leverage, liquidation price
- **`orders`** — Open orders with price, size, filled, status
- **`fills`** — Last 20 trade fills with price, size, fee, maker/taker
- **`income`** — Funding payment history with rates and indices
- **`all`** or no argument — Run everything above

## How to run

```bash
.venv/bin/python connectors/paradex_query.py $ARGUMENTS
```

## Presentation

- Format results in clean markdown tables
- Highlight: high funding rates (>10% annualized), large uPnL, positions near liquidation, negative funding on majors
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- Always show both per-period and annualized rates
- Taker fee is 2.0 bps (API), maker fee is 0.0 bps
- If repeated in the same session, note changes from previous query
