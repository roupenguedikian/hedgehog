---
name: paradex-limit
description: Place a limit order on Paradex DEX (StarkNet). Use when the user wants to buy, sell, long, short, or place an order on Paradex.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> <price> [--tif GTC|IOC|POST_ONLY] [--reduce-only]"
---

# Paradex Limit Order

Place a limit order on Paradex via the paradex-py SDK. Requires `PARADEX_L2_ADDRESS` and `PARADEX_L2_PRIVATE_KEY` in `.env`.

## CRITICAL: Always confirm before executing

**NEVER place an order without explicit user confirmation.** Before running the script:

1. Parse the user's intent into: symbol, side, size, price, TIF, reduce-only
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output to the user and ask: "Confirm this order?"
4. Only run WITHOUT `--dry-run` after the user explicitly confirms (e.g. "yes", "go", "confirm", "do it")

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — Bare symbol: BTC, ETH, SOL, etc. (auto-appended `-USD-PERP`)
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units (e.g. 0.001 BTC, 1.5 ETH, 100 SOL)
- **price** — limit price in USD
- **--tif** — time-in-force (optional, default GTC):
  - `GTC` — Good Til Cancel (rests on book)
  - `IOC` — Immediate or Cancel (fills what it can, cancels rest)
  - `POST_ONLY` — Maker only, rejected if would cross (0 bps fee)
- **--reduce-only** — only reduce an existing position (optional)

If the user doesn't specify all required fields, ask for the missing ones.

If the user says something like "market buy" or "market order", use `--tif IOC` with an aggressive price (5-10 bps above best ask for buys, below best bid for sells). First do a dry run to show the slippage.

## How to run

### Dry run (always do this first):

```bash
.venv/bin/python scripts/paradex_order.py <symbol> <side> <size> <price> [--tif GTC] [--reduce-only] --dry-run
```

### Execute (only after user confirms):

```bash
.venv/bin/python scripts/paradex_order.py <symbol> <side> <size> <price> [--tif GTC] [--reduce-only]
```

## Examples

```bash
# Dry run: limit buy 0.001 BTC at $70,000
.venv/bin/python scripts/paradex_order.py BTC long 0.001 70000 --dry-run

# Execute: limit sell 10 SOL at $95
.venv/bin/python scripts/paradex_order.py SOL short 10 95

# Maker-only buy 100 DOGE at $0.10
.venv/bin/python scripts/paradex_order.py DOGE long 100 0.10 --tif POST_ONLY

# Close a position (reduce only)
.venv/bin/python scripts/paradex_order.py ETH short 0.5 2200 --reduce-only
```

## Presentation

After dry run, show the user:
- Order details: symbol, side, size, price, notional, TIF
- Market context: mark price, best bid/ask, spread, how far the limit is from mid
- Account: NAV, free margin, whether the order is affordable
- Maker fee is 0.0 bps, taker fee is 2.0 bps — POST_ONLY guarantees maker pricing
- Ask for explicit confirmation

After execution, show:
- Order result: submitted/filled, order ID
- Updated account: new NAV, new free margin
- Any open orders for the symbol
