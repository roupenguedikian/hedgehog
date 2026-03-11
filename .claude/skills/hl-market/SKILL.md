---
name: hl-market
description: Place a market (taker) order on Hyperliquid for immediate fill. Use when the user wants to market buy/sell or get filled immediately on Hyperliquid.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> [--slippage-bps N] [--reduce-only]"
---

# Hyperliquid Market Order

Place a market (taker) order on Hyperliquid using IOC with slippage protection. Requires `HYPERLIQUID_PRIVATE_KEY` in `.env`.

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
.venv/bin/python scripts/hl_market_order.py <symbol> <side> <size> [--slippage-bps 10] [--reduce-only] --dry-run
```

### Execute (after confirmation):
```bash
.venv/bin/python scripts/hl_market_order.py <symbol> <side> <size> [--slippage-bps 10] [--reduce-only]
```

## Presentation

After dry run: show symbol, side, size, slippage cap, aggressive limit price, best bid/ask, spread, account NAV/margin.
After execution: show fill qty, avg price, actual slippage in bps, unfilled remainder, updated account.
