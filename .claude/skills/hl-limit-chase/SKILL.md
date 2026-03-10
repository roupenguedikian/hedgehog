---
name: hl-limit-chase
description: Chase best bid/ask on Hyperliquid with continuous re-pricing. Use when the user wants to buy at best bid or sell at best ask with maker-only (ALO) orders that follow the top of book.
allowed-tools: Bash
argument-hint: "<symbol> <long|short> <size> [--reduce-only] [--max-iterations N] [--interval S] [--dry-run]"
---

# Hyperliquid Limit Chase

Continuously re-prices a limit order to stay at best bid (for buys) or best ask (for sells) using ALO (Add Liquidity Only) to guarantee maker fills.

## CRITICAL: Always confirm before executing

**NEVER start a chase without explicit user confirmation.** Before running the script:

1. Parse the user's intent into: symbol, side, size, and any options
2. First run with `--dry-run` to show order details + market context
3. Present the dry run output to the user and ask: "Confirm this chase?"
4. Only run WITHOUT `--dry-run` after the user explicitly confirms (e.g. "yes", "go", "confirm", "do it")

## How it works

- **BUY/LONG**: Places ALO limit at best bid, re-prices every interval to stay best bid
- **SELL/SHORT**: Places ALO limit at best ask, re-prices every interval to stay best ask
- Uses ALO (Add Liquidity Only) so orders never cross the spread — guaranteed maker fills
- Cancels and re-places if the order is no longer at the top of book
- Tracks partial fills and adjusts remaining size
- Stops when fully filled or max iterations reached

## Arguments

Parse `$ARGUMENTS` for:

- **symbol** — Hyperliquid bare symbol: BTC, ETH, SOL, DOGE, HYPE, etc.
- **side** — `long` (or `buy`/`b`) to buy, `short` (or `sell`/`s`) to sell
- **size** — quantity in base asset units (e.g. 0.001 BTC, 1.5 ETH, 100 SOL)
- **--reduce-only** — only reduce an existing position (optional)
- **--max-iterations N** — max re-price attempts (default: 100)
- **--interval S** — seconds between checks (default: 2)

If the user doesn't specify all required fields (symbol, side, size), ask for the missing ones.

## How to run

### Dry run (always do this first):

```bash
.venv/bin/python scripts/hl_limit_chase.py <symbol> <side> <size> [--reduce-only] [--max-iterations N] [--interval S] --dry-run
```

### Execute (only after user confirms):

```bash
.venv/bin/python scripts/hl_limit_chase.py <symbol> <side> <size> [--reduce-only] [--max-iterations N] [--interval S]
```

## Examples

```bash
# Dry run: chase best bid for 0.001 BTC
.venv/bin/python scripts/hl_limit_chase.py BTC long 0.001 --dry-run

# Chase best ask to sell 0.5 ETH (reduce only)
.venv/bin/python scripts/hl_limit_chase.py ETH short 0.5 --reduce-only

# Chase best bid for 10 SOL, check every 3s, max 50 iterations
.venv/bin/python scripts/hl_limit_chase.py SOL long 10 --interval 3 --max-iterations 50
```

## Presentation

After dry run, show the user:
- Chase details: symbol, side, size, initial target price, estimated notional
- Market context: best bid/ask, spread, ALO guarantee
- Account: NAV, free margin
- Ask for explicit confirmation

During execution (if user watches output):
- Iteration log showing: action (PLACE/REPRICE/HOLD/FILLED/PARTIAL/REJECTED), price, filled, remaining, bid, ask

After completion, show:
- Chase summary: total filled, average price, total cost, iterations used
- Updated account: NAV, free margin
