---
name: aster
description: Query Aster DEX for funding rates, account balance, margin, positions, open orders, trade fills, and funding payments. Use when the user asks about Aster positions, balances, funding rates, or trading activity.
allowed-tools: Bash
argument-hint: "[funding|account|positions|orders|fills|income|all]"
---

# Aster DEX Query

Query the Aster DEX API for live market and account data.

Aster uses a Binance-compatible API at `https://fapi.asterdex.com`. Symbols use `{symbol}USDT` format. Funding cycle is 8 hours (1,095 payments/year). Public data (funding) needs no auth. Account data requires ASTER_API_KEY and ASTER_API_SECRET in `.env`.

## What to query

Based on `$ARGUMENTS`, run the appropriate section(s). Default is `all`.

- **`funding`** — Top 20 funding rates by 24h volume + extreme funding scanner (public, no auth)
- **`account`** — Wallet balance, unrealized PnL, margin balance, margin used/free, max withdraw
- **`positions`** — Open positions with entry, mark, PnL, leverage, liquidation price
- **`orders`** — Open orders with price, qty, filled, TIF
- **`fills`** — Last 20 trade fills with price, qty, fee, maker/taker
- **`income`** — Funding payments + other income (realized PnL, commissions), totals by type
- **`all`** or no argument — Run everything above

## How to run

```bash
.venv/bin/python scripts/aster_query.py $ARGUMENTS
```

## Presentation

- Format results in clean markdown tables
- Highlight: high funding rates (>10% annualized), large uPnL, positions near liquidation, negative funding on majors
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- Always show both per-period and annualized rates
- If repeated in the same session, note changes from previous query
