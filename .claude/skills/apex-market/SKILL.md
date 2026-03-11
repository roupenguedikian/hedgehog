---
name: apex-market
description: Place a market (taker) order on Apex Omni for immediate fill. Use when the user wants to market buy/sell or get filled immediately on Apex.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> [--slippage-bps N] [--reduce-only]"
---

# Apex Omni Market Order

Place a MARKET order on Apex Omni via the apexomni SDK. Requires `APEX_OMNI_API_KEY`, `APEX_OMNI_API_SECRET`, `APEX_OMNI_PASSPHRASE`, and `APEX_OMNI_L2_KEY` in `.env`.

## CRITICAL: Always confirm before executing

**NEVER place an order without explicit user confirmation.** Before running:

1. Parse the user's intent into: symbol, side, size, slippage
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output and ask: "Confirm this market order?"
4. Only run WITHOUT `--dry-run` after explicit confirmation

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — BTC, ETH, SOL, etc. (USDT auto-appended)
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units
- **--slippage-bps N** — max slippage in basis points (default: 50)
- **--reduce-only** — only reduce an existing position

## How to run

### Dry run (always first):
```bash
.venv/bin/python scripts/apex_market_order.py <symbol> <side> <size> [--slippage-bps 50] [--reduce-only] --dry-run
```

### Execute (after confirmation):
```bash
.venv/bin/python scripts/apex_market_order.py <symbol> <side> <size> [--slippage-bps 50] [--reduce-only]
```

## Presentation

After dry run: show symbol, side, size, slippage cap, worst price, estimated notional, last price, funding, account.
After execution: show status, order ID, avg price, actual slippage, updated account.
