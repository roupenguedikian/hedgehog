---
name: lighter-market
description: Place a market (taker) order on Lighter DEX for immediate fill. Use when the user wants to market buy/sell or get filled immediately on Lighter.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> [--slippage-bps N] [--reduce-only]"
---

# Lighter ZK-Rollup Market Order

Place a market order on Lighter using IOC with slippage protection via the lighter-sdk. Requires `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`, and `LIGHTER_API_KEY_PRIVATE_KEY` in `.env`. Zero fees.

## CRITICAL: Always confirm before executing

**NEVER place an order without explicit user confirmation.** Before running:

1. Parse the user's intent into: symbol, side, size, slippage
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output and ask: "Confirm this market order?"
4. Only run WITHOUT `--dry-run` after explicit confirmation

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — BTC, ETH, SOL, etc. (bare symbols)
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units
- **--slippage-bps N** — max slippage in basis points (default: 10)
- **--reduce-only** — only reduce an existing position

## How to run

### Dry run (always first):
```bash
.venv/bin/python scripts/lighter_market_order.py <symbol> <side> <size> [--slippage-bps 10] [--reduce-only] --dry-run
```

### Execute (after confirmation):
```bash
.venv/bin/python scripts/lighter_market_order.py <symbol> <side> <size> [--slippage-bps 10] [--reduce-only]
```

## Presentation

After dry run: show symbol, side, size, slippage cap, aggressive IOC price, best bid/ask, spread, funding, account.
After execution: show status, order ID, updated account.
