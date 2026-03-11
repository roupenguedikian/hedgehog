---
name: lighter
description: Query Lighter ZK-rollup for account balance, positions, funding payments, and market data. Use when the user asks about Lighter positions, balances, funding rates, markets, or account status.
allowed-tools: Bash(python3 *)
argument-hint: "[account|positions|funding|orders|fills|margin|markets|all] [account_index]"
---

# Lighter ZK-Rollup Query

Query the Lighter DEX API for live account and market data. Credentials are in `.env` (LIGHTER_API_KEY_PRIVATE_KEY, LIGHTER_API_KEY_INDEX, LIGHTER_ACCOUNT_INDEX).

## What to query

Based on `$ARGUMENTS`, run the appropriate subcommand:

- **`account`** — Account balance summary: collateral, available, equity, margin utilization
- **`positions`** — Open positions with side, size, entry price, value, unrealized PnL, liquidation price, IMF
- **`margin`** — Detailed margin breakdown per position (margin required, IMF, buffer %) + account tier/fees
- **`orders`** — Open orders across all markets (authenticated)
- **`funding`** — Funding payment history (authenticated): per-position totals, $/hour, $/day, annualized rate
- **`fills`** — Recent trade fills with date, side, size, price, value, closed PnL, role (via export CSV)
- **`markets`** — All active perpetual markets with price, 24h change, open interest, volume
- **`all`** or no argument — Run everything above (except markets)

An optional account index (integer) can be passed to override the default from .env.

## How to run

```
python3 connectors/lighter_query.py $ARGUMENTS
```

## Presentation

- Format results in clean markdown tables
- Highlight anything notable: large unrealized PnL, high funding rates (>10% annualized), positions near liquidation
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- For funding rates, always show both per-period and annualized
- Mark current open positions with `<` in the funding table
- Flag negative funding on major coins (potential long opportunity for the arb strategy)
- If repeated in the same session, note changes from previous query
