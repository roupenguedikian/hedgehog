---
name: dydx
description: Query dYdX v4 for funding rates, market prices, account balance, margin used/free, positions, open orders, trade fills, and funding payments. Use when the user asks about dYdX positions, balances, funding rates, markets, or trading activity.
allowed-tools: Bash
argument-hint: "[funding|markets|account|positions|orders|fills|transfers|all] [address]"
---

# dYdX v4 Query

Query the dYdX v4 indexer API for live market and account data.

dYdX v4 uses a public indexer at `https://indexer.dydx.trade/v4`. Symbols use `{symbol}-USD` format. Funding cycle is 1 hour (8,760 payments/year). No auth needed — market data is public, account data needs a dYdX address (from `.env` DYDX_WALLET_ADDRESS or passed as argument).

## What to query

Based on `$ARGUMENTS`, run the appropriate section(s). Default is `all`.

- **`markets`** — Top 20 perpetual markets by volume with funding rate, annualized %, OI, volume + extreme funding scanner
- **`funding`** — Recent funding history (last 5 entries) for top 15 symbols
- **`account`** — Equity (NAV), margin used, free margin, asset positions per subaccount
- **`positions`** — Open perpetual positions with side, size, entry price, unrealized/realized PnL
- **`orders`** — Open limit orders with side, size, price, type, status
- **`fills`** — Last 10 trade fills with timestamp, price, size, fee
- **`transfers`** — Recent funding payments and transfers
- **`all`** or no argument — Run everything above

An optional dYdX address (starts with `dydx1`) can be passed to override the default from `.env`.

## How to run

```bash
.venv/bin/python connectors/dydx_query.py $ARGUMENTS
```

## Presentation

- Format results in clean markdown tables
- Highlight: high funding rates (>10% annualized), large uPnL, negative funding on majors
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- Always show both per-period and annualized rates
- If repeated in the same session, note changes from previous query
