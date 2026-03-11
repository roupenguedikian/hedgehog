---
name: ethereal
description: Query Ethereal DEX for funding rates, account balance, margin, positions, open orders, trade fills, and funding payments. Use when the user asks about Ethereal positions, balances, funding rates, or trading activity.
allowed-tools: Bash
argument-hint: "[funding|account|positions|orders|fills|income|all] [address]"
---

# Ethereal DEX Query

Query the Ethereal DEX API for live market and account data.

Ethereal runs on the Converge chain (EVM) with USDe collateral. API base is `https://api.ethereal.trade`. Symbols use `{symbol}-USD` format (displayTicker). Funding is hourly (`fundingRate1h` field). No API key auth — market data is public, account data needs an EVM wallet address (from `.env` ETHEREAL_WALLET_ADDRESS or passed as argument) to resolve subaccounts.

## What to query

Based on `$ARGUMENTS`, run the appropriate section(s). Default is `all`.

- **`funding`** — All products with current 1h funding rate, annualized %, mark/index prices, OI, volume + extreme funding scanner + projected rates
- **`account`** — Subaccount balance (USDe), available margin, margin used
- **`positions`** — Open positions with entry, mark, PnL, leverage, liquidation price, margin
- **`orders`** — Open orders with price, qty, filled, type, TIF, status
- **`fills`** — Last 20 trade fills with price, size, fee, side
- **`income`** — Funding rate history for positions (last day)
- **`all`** or no argument — Run everything above

An optional wallet address (0x...) can be passed to override the default from `.env`.

## How to run

```bash
.venv/bin/python connectors/ethereal_query.py $ARGUMENTS
```

## Presentation

- Format results in clean markdown tables
- Highlight: high funding rates (>10% annualized), large uPnL, positions near liquidation, negative funding on majors
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- Always show both per-period and annualized rates
- Note that Ethereal uses USDe (yield-bearing) collateral — mention embedded yield when discussing account balance
- If repeated in the same session, note changes from previous query
