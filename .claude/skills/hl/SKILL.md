---
name: hl
description: Query Hyperliquid account positions, balance, open orders, recent fills, and top funding rates. Use when the user asks about Hyperliquid positions, balance, PnL, account status, or funding rates.
argument-hint: "[address]"
allowed-tools: Bash
---

# Hyperliquid Account & Market Query

Query a Hyperliquid address for positions, balance, open orders, recent fills, and funding rates for the top 20 symbols by volume.

## Address

- If `$ARGUMENTS` is provided and looks like an Ethereum address (starts with `0x`), use that address.
- Otherwise read `HYPERLIQUID_WALLET_ADDRESS` from `.env` (use `grep ^HYPERLIQUID_WALLET_ADDRESS .env | cut -d= -f2`).
- If neither is available, ask the user for an address.

## How to query

Run this command from the project root, substituting `ADDRESS` with the resolved address:

```bash
.venv/bin/python connectors/hl_query.py $ARGUMENTS
```

Subcommands: `balance`, `positions`, `orders`, `fills`, `funding`, `all` (default).
An optional Ethereum address can be passed to override the default from `.env`.

## Output format

Summarize results in clean tables:

1. **Balance** — Account Value (NAV), USDC Balance (raw deposit), Position Notional, Margin Used, Maintenance Margin, Free Margin (NAV - margin), Withdrawable. Note: NAV = raw USDC + unrealized position value, so raw USDC can be negative while NAV is positive
2. **Positions** — symbol, side, size, notional, entry, uPnL, leverage, liquidation price, total uPnL
3. **Open Orders** — symbol, side, price, size (or "None")
4. **Last 5 Fills** — symbol, side, price, size, fee (or "No fills")
5. **Top 20 Funding Rates** — ranked by 24h volume, showing rate/hr, annualized %, premium, mark price, open interest, volume
6. **Extreme Funding** — top 5 highest and top 5 most negative annualized rates across all assets (arb opportunity scanner)

Keep it concise. If the user has been running `/hl` repeatedly in this session, note any changes from the previous query — especially position/balance deltas and any significant funding rate movements.
