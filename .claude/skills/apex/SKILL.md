---
name: apex
description: Query Apex Omni (zkLink) for funding rates, account balance, margin, positions, open orders, trade fills, order history, PnL, and transfers. Use when the user asks about Apex positions, balances, funding rates, or trading activity.
allowed-tools: Bash
argument-hint: "[funding|account|positions|orders|fills|history|pnl|income|all]"
---

# Apex Omni Query

Query the Apex Omni exchange API for live market and account data.

Apex Omni is a multi-chain DEX built on zkLink X. API base is `https://omni.apex.exchange`. Symbols use `{symbol}USDT` format. Funding cycle is 8 hours (1,095 payments/year). Public data (funding) needs no auth. Account data requires APEX_OMNI_API_KEY, APEX_OMNI_API_SECRET, and APEX_OMNI_PASSPHRASE in `.env`. Auth uses HMAC-SHA256 with the secret base64-encoded as the HMAC key.

## What to query

Based on `$ARGUMENTS`, run the appropriate section(s). Default is `all`.

- **`funding`** — Funding rates for ~30 symbols sorted by OI + extreme funding scanner (public, no auth)
- **`account`** — Account ID, address, fee rates, equity, available balance, margin, unrealized/realized PnL
- **`positions`** — Open positions with side, size, entry price, unrealized PnL, liquidation price
- **`orders`** — Open orders with type, price, size, status
- **`fills`** — Last 20 trade fills with timestamp, price, size, fee, maker/taker direction
- **`history`** — Last 20 historical orders with status (filled, canceled, etc.)
- **`pnl`** — Historical PnL from closed positions with entry/exit prices and net totals
- **`income`** — Funding payments received/paid on positions + recent funding rate history for BTC/ETH/SOL
- **`all`** or no argument — Run everything above

## How to run

```bash
python3 scripts/apex_query.py $ARGUMENTS
```

## Presentation

- Format results in clean markdown tables
- Highlight: high funding rates (>10% annualized), large uPnL, positions near liquidation, negative funding on majors
- Compare funding rates to strategy.yaml min_spread (10% annual) when relevant
- Always show both per-period (8h) and annualized rates
- Note that Apex Omni uses 8h funding cycles (multiply rate by 1,095 for annual)
- If repeated in the same session, note changes from previous query
