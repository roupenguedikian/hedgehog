---
name: aster-market
description: Place a market (taker) order on Aster DEX for immediate fill. Use when the user wants to market buy/sell or get filled immediately on Aster.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> [--reduce-only]"
---

# Aster DEX Market Order

Place a MARKET order on Aster DEX for immediate execution. Requires `ASTER_API_KEY` and `ASTER_API_SECRET` in `.env`.

## CRITICAL: Always confirm before executing

**NEVER place an order without explicit user confirmation.** Before running:

1. Parse the user's intent into: symbol, side, size
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output and ask: "Confirm this market order?"
4. Only run WITHOUT `--dry-run` after explicit confirmation

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — BTC, ETH, SOL, etc. (USDT auto-appended)
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units
- **--reduce-only** — only reduce an existing position

## How to run

### Dry run (always first):
```bash
.venv/bin/python scripts/aster_market_order.py <symbol> <side> <size> [--reduce-only] --dry-run
```

### Execute (after confirmation):
```bash
.venv/bin/python scripts/aster_market_order.py <symbol> <side> <size> [--reduce-only]
```

## Presentation

After dry run: show symbol, side, size, estimated price, estimated notional, best bid/ask, spread, funding, account.
After execution: show status, fill qty, avg price, actual slippage, updated account.
