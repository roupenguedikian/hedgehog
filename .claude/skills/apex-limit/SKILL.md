---
name: apex-limit
description: Place a limit order on Apex Omni. Use when the user wants to buy, sell, long, short, or place an order on Apex.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only]"
---

# Apex Omni Limit Order

Place a limit order on Apex Omni (zkLink multi-chain) via the apexomni SDK. Requires `APEX_OMNI_API_KEY`, `APEX_OMNI_API_SECRET`, `APEX_OMNI_PASSPHRASE`, and `APEX_OMNI_L2_KEY` in `.env`.

## CRITICAL: Always confirm before executing

**NEVER place an order without explicit user confirmation.** Before running the script:

1. Parse the user's intent into: symbol, side, size, price, TIF, reduce-only
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output to the user and ask: "Confirm this order?"
4. Only run WITHOUT `--dry-run` after the user explicitly confirms (e.g. "yes", "go", "confirm", "do it")

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — Base asset: BTC, ETH, SOL, DOGE, etc. (USDT is auto-appended → BTCUSDT)
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units (e.g. 0.001 BTC, 1.5 ETH, 100 SOL)
- **price** — limit price in USD
- **--tif** — time-in-force (optional, default GTC):
  - `GTC` — Good Til Cancel (rests on book)
  - `IOC` — Immediate or Cancel (fills what it can, cancels rest)
  - `GTX` — Post Only (maker only, rejected if would cross)
- **--reduce-only** — only reduce an existing position (optional)

If the user doesn't specify all required fields, ask for the missing ones.

## How to run

### Dry run (always do this first):

```bash
.venv/bin/python scripts/apex_order.py <symbol> <side> <size> <price> [--tif GTC] [--reduce-only] --dry-run
```

### Execute (only after user confirms):

```bash
.venv/bin/python scripts/apex_order.py <symbol> <side> <size> <price> [--tif GTC] [--reduce-only]
```

## Examples

```bash
# Dry run: limit buy 0.001 BTC at $70,000
.venv/bin/python scripts/apex_order.py BTC long 0.001 70000 --dry-run

# Execute: limit sell 10 SOL at $95
.venv/bin/python scripts/apex_order.py SOL short 10 95

# Post-only buy 100 DOGE at $0.10
.venv/bin/python scripts/apex_order.py DOGE long 100 0.10 --tif GTX

# Close a position (reduce only)
.venv/bin/python scripts/apex_order.py ETH short 0.5 2200 --reduce-only
```

## Presentation

After dry run, show the user:
- Order details: symbol, side, size, price, notional, TIF
- Market context: last price, index price, funding rate (8h cycle), predicted rate, best bid/ask, spread
- Account: NAV, free margin
- Ask for explicit confirmation

After execution, show:
- Order result: status, order ID
- Updated account: new NAV, new free margin
- Any open orders for the symbol
