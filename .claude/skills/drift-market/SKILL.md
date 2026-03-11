---
name: drift-market
description: Place a market (taker) order on Drift Protocol for immediate fill. Use when the user wants to market buy/sell or get filled immediately on Drift.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> [--slippage-bps N] [--reduce-only]"
---

# Drift Protocol Market Order

Place a market order on Drift Protocol using Drift Gateway (preferred) or driftpy SDK (fallback). Requires `DRIFT_GATEWAY_URL` or `DRIFT_PRIVATE_KEY` in `.env`. 0 bps maker fee, 3 bps taker fee.

## CRITICAL: Always confirm before executing

**NEVER place an order without explicit user confirmation.** Before running:

1. Parse the user's intent into: symbol, side, size, slippage
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output and ask: "Confirm this market order?"
4. Only run WITHOUT `--dry-run` after explicit confirmation

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — BTC, ETH, SOL, etc. (-PERP auto-appended)
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units
- **--slippage-bps N** — max slippage in basis points (default: 20)
- **--reduce-only** — only reduce an existing position

## How to run

### Dry run (always first):
```bash
.venv/bin/python scripts/drift_market_order.py <symbol> <side> <size> [--slippage-bps 20] [--reduce-only] --dry-run
```

### Execute (after confirmation):
```bash
.venv/bin/python scripts/drift_market_order.py <symbol> <side> <size> [--slippage-bps 20] [--reduce-only]
```

## Presentation

After dry run: show symbol, side, size, slippage cap, IOC limit price, oracle price, best bid/ask from DLOB, spread, funding, account health.
After execution: show status, order ID or tx signature, updated account.
