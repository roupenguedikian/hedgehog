---
name: edgex
description: Query EdgeX DEX for funding rates, market prices, account balance, positions, open orders, trade fills, and funding payments. Use when the user asks about EdgeX positions, balances, funding rates, markets, or trading activity.
allowed-tools: Bash(python3 *)
argument-hint: "[funding|markets|account|positions|orders|fills|income|all] [account_id]"
---

# EdgeX DEX Query

Query the EdgeX DEX API for live account and market data. Credentials are in `.env` (EDGEX_ACCOUNT_ID, EDGEX_PRIVATE_KEY).

## What to query

Based on the user's request, run the appropriate subcommand:

- **`funding`** — Funding rates for all markets: rate/4h, annualized, mark price, OI, volume, extreme funding scanner
- **`markets`** — All active markets with price, 24h change, volume, trades
- **`account`** — Account balance: equity, available margin, margin utilization, collateral
- **`positions`** — Open positions with side, size, value, entry, mark, uPnL
- **`orders`** — Active open orders
- **`fills`** — Recent trade fills with price, value, PnL, maker/taker role
- **`income`** — Funding payment history aggregated by symbol + last 10 individual payments
- **`all`** or no argument — Run everything above

An optional account ID (long integer) can be passed to override the default from .env.

## How to run

```
python3 connectors/edgex_query.py <subcommand>
```

## Key Details

- Funding cycle: **4 hours** (240 min), 2190 payments/year
- Taker fee: 3.8 bps (majors), 4.8 bps (smaller pairs)
- Maker fee: 1.8 bps
- Public endpoints (funding, markets) need no auth
- Private endpoints require EDGEX_PRIVATE_KEY (secp256k1 ECDSA + SHA3 signing)

## Presentation

- Format results in clean markdown tables
- Highlight extreme rates (>10% annualized), large PnL
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- For funding rates, always show both per-period and annualized
