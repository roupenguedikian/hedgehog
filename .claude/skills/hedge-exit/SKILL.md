---
name: hedge-exit
description: Check which hedge positions should be closed. Use when the user wants to see if any existing hedges have degraded below the exit threshold.
allowed-tools: Bash
argument-hint: "[--symbols BTC,ETH,SOL]"
---

# Hedge Exit Scanner

Check all current hedge positions and flag any that should be closed because their daily funding yield has dropped below the exit threshold.

## How it works

1. Fetches current funding rates from all 6 venues in parallel
2. Fetches open positions from all venues
3. Identifies **hedge pairs** — same symbol, SHORT on one venue, LONG on another
4. For each hedge pair, calculates current DPY and flags for exit if `DPY < EXIT_NDPY`

## Exit logic

A hedge should be exited when the funding rate spread has compressed to the point where:
- The daily yield no longer justifies holding the position
- DPY drops below the `EXIT_NDPY` threshold

Since entry fees are a sunk cost, the exit decision is based purely on whether the ongoing DPY justifies continued holding. Breakeven does not apply for exits.

## Thresholds (from .env)

- `EXIT_NDPY` — minimum DPY to keep a position open (default 0.0003 = 0.03%)

## How to run

```bash
.venv/bin/python scripts/hedge_scanner.py exit
```

Optional: filter to specific symbols:
```bash
.venv/bin/python scripts/hedge_scanner.py exit --symbols=BTC,ETH
```

## Presentation

After running, present to the user:

### 1. Hedge Status Table
| Symbol | Short@Venue | Long@Venue | DPY | NDPY | Size | Status |

Status is either:
- **HOLD** (green) — DPY is above exit threshold, keep position
- **EXIT** (red) — DPY below threshold, recommend closing

### 2. Unmatched Positions
Any positions without a matching opposite leg (orphaned legs that need attention).

### 3. Actionable Advice
For positions flagged EXIT:
- "Close SHORT on [venue] via /[venue]-market [symbol] long [size] (reduce-only)"
- "Close LONG on [venue] via /[venue]-market [symbol] short [size] (reduce-only)"
- Remind that all orders are taker orders with --reduce-only flag
- Suggest closing both legs promptly to avoid directional exposure
