---
name: hedge-rotation
description: Check if current hedge positions can be rotated to better venues. Use when the user wants to optimize existing positions by moving a leg to a venue with better funding rates.
allowed-tools: Bash
argument-hint: "[--symbols BTC,ETH,SOL]"
---

# Hedge Rotation Scanner

Check all current positions across 6 venues and determine if any hedge leg can be moved to a venue with a better funding rate, improving the overall yield.

## How it works

1. Fetches current funding rates from all 6 venues in parallel
2. Fetches open positions from all venues (uses wallet addresses / API keys from .env)
3. Identifies **hedge pairs** — same symbol, SHORT on one venue, LONG on another
4. For each hedge pair, checks if moving the short or long leg to a different venue would:
   - **Improve NDPY** (the new venue must offer a strictly better rate)
   - **Break even in <= ROTATION_BREAKEVEN hours** (the cost of closing one leg and opening on the new venue must be recouped quickly)

## Rotation cost calculation

Rotating one leg means:
- **Close** the existing leg (pay taker fee on old venue)
- **Open** the same position on the new venue (pay taker fee on new venue)
- Rotation fee = old_venue_taker + new_venue_taker
- Rotation breakeven = 24 * rotation_fee / DPY_improvement

## Thresholds (from .env)

- `ROTATION_NDPY` — new NDPY just has to be better than current (default 0)
- `ROTATION_BREAKEVEN` — max hours to break even on rotation cost (default 6)

## How to run

```bash
.venv/bin/python scripts/hedge_scanner.py rotation
```

Optional: filter to specific symbols:
```bash
.venv/bin/python scripts/hedge_scanner.py rotation --symbols=BTC,ETH
```

## Presentation

After running, present to the user:

### 1. Current Positions
Table of all open positions across venues.

### 2. Rotation Opportunities
For each rotation opportunity, show:
| Symbol | Leg | From | To | Current DPY | New DPY | Improvement | Rotation Fee | BE(h) |

### 3. Actionable Advice
For qualifying rotations, suggest the sequence:
1. "Close [side] on [old venue] via /[venue]-market"
2. "Open [side] on [new venue] via /[venue]-market"
- Remind that both orders are taker orders
- Suggest executing quickly to minimize exposure gap
