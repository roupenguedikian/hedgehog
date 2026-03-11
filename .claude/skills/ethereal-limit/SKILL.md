---
name: ethereal-limit
description: Place a limit order on Ethereal DEX. Use when the user wants to buy, sell, long, short, or place an order on Ethereal.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> <price> [--tif GTC|IOC|FOK] [--reduce-only] [--post-only]"
---

# Ethereal DEX Limit Order

Place a limit order on Ethereal DEX (Converge chain, EVM) via the official `ethereal-sdk`. Requires `ETHEREAL_PRIVATE_KEY` in `.env`. Collateral is USDe (yield-bearing). Funding cycle is 1h. Maker fee: 2 bps, taker fee: 3 bps.

## CRITICAL: Always confirm before executing

**NEVER place an order without explicit user confirmation.** Before running the script:

1. Parse the user's intent into: symbol, side, size, price, TIF, reduce-only, post-only
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output to the user and ask: "Confirm this order?"
4. Only run WITHOUT `--dry-run` after the user explicitly confirms (e.g. "yes", "go", "confirm", "do it")

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — Base asset: BTC, ETH, SOL, HYPE, SUI, etc. (USD is auto-appended → BTCUSD)
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units (e.g. 0.01 BTC, 1.5 ETH, 100 SOL)
- **price** — limit price in USD
- **--tif** — time-in-force (optional, default GTC):
  - `GTC` — Good Til Cancel (maps to GTD, rests on book)
  - `IOC` — Immediate or Cancel (fills what it can, cancels rest)
  - `FOK` — Fill or Kill (fills entirely or cancels)
- **--post-only** — maker only, rejected if would cross (optional)
- **--reduce-only** — only reduce an existing position (optional)

If the user doesn't specify all required fields, ask for the missing ones.

If the user says something like "market buy" or "market order", use `--tif IOC` with an aggressive price (5-10 bps above best ask for buys, below best bid for sells). First do a dry run to show the slippage.

## How to run

### Dry run (always do this first):

```bash
.venv/bin/python scripts/ethereal_order.py <symbol> <side> <size> <price> [--tif GTC] [--post-only] [--reduce-only] --dry-run
```

### Execute (only after user confirms):

```bash
.venv/bin/python scripts/ethereal_order.py <symbol> <side> <size> <price> [--tif GTC] [--post-only] [--reduce-only]
```

## Examples

```bash
# Dry run: limit buy 0.01 BTC at $80,000
.venv/bin/python scripts/ethereal_order.py BTC long 0.01 80000 --dry-run

# Execute: limit sell 1 ETH at $2,500
.venv/bin/python scripts/ethereal_order.py ETH short 1 2500

# Post-only buy 10 SOL at $120
.venv/bin/python scripts/ethereal_order.py SOL long 10 120 --post-only

# Close a position (reduce only)
.venv/bin/python scripts/ethereal_order.py ETH short 0.5 2200 --reduce-only
```

## Presentation

After dry run, show the user:
- Order details: symbol, side, size, price, notional, TIF, post-only, reduce-only
- Market context: mark price, oracle price, funding rate, best bid/ask, spread, how far the limit is from mid
- Account: NAV, free margin, whether the order is affordable
- Ask for explicit confirmation

After execution, show:
- Order result: status, order ID, filled amount if any
- Updated account: new NAV, new free margin
- Any open orders for the symbol
