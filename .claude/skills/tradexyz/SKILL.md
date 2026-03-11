---
name: tradexyz
description: Query trade.xyz (XYZ) HIP-3 DEX for funding rates, market prices, account balance, positions, open orders, trade fills, and funding payments. Use when the user asks about trade.xyz, XYZ, or XYZ equity/crypto perp positions, balances, funding rates, or trading activity.
allowed-tools: Bash(python3 *)
argument-hint: "[funding|markets|account|positions|orders|fills|income|all] [address]"
---

# trade.xyz (XYZ) Query

Query the trade.xyz HIP-3 DEX on Hyperliquid for live account and market data. XYZ is a HIP-3 builder DEX supporting equity perpetuals (stocks like NVDA, TSLA, AAPL), index contracts (XYZ100), and crypto perpetuals. Credentials are in `.env` (TRADEXYZ_WALLET_ADDRESS or HYPERLIQUID_WALLET_ADDRESS).

## What to query

Based on the user's request, run the appropriate subcommand:

- **`funding`** — Funding rates for all XYZ markets: rate/1h, annualized, premium, mark price, OI, volume, extreme funding scanner
- **`markets`** — All active XYZ markets with mark price, oracle price, OI, volume, max leverage
- **`account`** — Account balance on XYZ clearinghouse: NAV, USDC balance, margin used/free, maintenance margin, unrealized PnL
- **`positions`** — Open positions on XYZ markets with side, size, notional, entry, mark, uPnL, leverage, liquidation price
- **`orders`** — Active open orders on XYZ markets
- **`fills`** — Recent trade fills on XYZ markets with price, value, fee, maker/taker role
- **`income`** — Funding payment history on XYZ markets, aggregated by symbol + individual payments
- **`all`** or no argument — Run everything above

An optional wallet address (0x...) can be passed to override the default from .env.

## How to run

```
python3 connectors/tradexyz_query.py <subcommand>
```

## Key Details

- HIP-3 DEX on Hyperliquid (same API at api.hyperliquid.xyz, separate clearinghouse)
- Funding cycle: **1 hour** (8760 payments/year)
- Supports equity perps (NVDA, TSLA, AAPL, GOOGL, AMZN, MSFT, META, PLTR), index contracts (XYZ100), and crypto perps
- Wallet address is the same Hyperliquid address (shared chain); falls back to HYPERLIQUID_WALLET_ADDRESS
- Taker fee: <0.9 bps (Growth Mode), Maker fee: 0 bps (rebate)
- Public endpoints (funding, markets) need no auth
- Private endpoints need a wallet address only (public indexer data, no signing required)

## Presentation

- Format results in clean markdown tables
- Highlight extreme rates (>10% annualized), large PnL
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- For funding rates, always show both per-period and annualized
- If repeated in the same session, note changes from the previous query
