---
name: extended
description: Query Extended DEX (Starknet) for funding rates, market prices, account balance, positions, open orders, trade fills, and funding payments. Use when the user asks about Extended positions, balances, funding rates, markets, or trading activity.
allowed-tools: Bash(python3 *)
argument-hint: "[funding|markets|account|positions|orders|fills|income|all]"
---

# Extended DEX Query

Query the Extended DEX API (Starknet L2, formerly X10) for live account and market data. Credentials are in `.env` (EXTENDED_API_KEY).

## What to query

Based on the user's request, run the appropriate subcommand:

- **`funding`** — Funding rates for all markets: rate/1h, annualized, mark price, OI, volume, extreme funding scanner
- **`markets`** — All active markets with price, 24h change, volume, trades
- **`account`** — Account balance: equity, available margin, margin utilization, unrealized PnL
- **`positions`** — Open positions with side, size, value, entry, mark, uPnL
- **`orders`** — Active open orders
- **`fills`** — Recent trade fills with price, value, fee, maker/taker role
- **`income`** — Funding payment history aggregated by symbol + last 10 individual payments
- **`all`** or no argument — Run everything above

## How to run

```
python3 connectors/extended_query.py <subcommand>
```

## Key Details

- Chain: **Starknet** (ZK-rollup L2)
- Funding cycle: **1 hour** (8h rate / 8), 8760 payments/year
- Taker fee: 2.5 bps
- Maker fee: 0 bps (free)
- Symbol format: `{symbol}-USD` (e.g., BTC-USD, ETH-USD)
- Public endpoints (funding, markets) need no auth
- Private endpoints require EXTENDED_API_KEY (X-Api-Key header)
- Write operations (orders) additionally require Stark signature (SNIP12)
- API base: `https://api.starknet.extended.exchange/api/v1`
- Max leverage: 100x (varies by market group)
- Collateral: USDC

## Presentation

- Format results in clean markdown tables
- Highlight extreme rates (>10% annualized), large PnL
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- For funding rates, always show both per-period and annualized
