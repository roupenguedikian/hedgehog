---
name: hedge-entry
description: Scan all 6 venues for new hedge entry opportunities. Use when the user wants to find the best funding rate arbitrage pairs to enter.
allowed-tools: Bash
argument-hint: "[--symbols BTC,ETH,SOL]"
---

# Hedge Entry Scanner

Scan all 6 venues (HL, Aster, Lighter, Apex, dYdX, Drift) for funding rate arbitrage opportunities that meet entry thresholds.

## How it works

For every target symbol, the scanner:
1. Fetches current funding rates from all 6 venues in parallel
2. Finds the **best short venue** (highest rate — shorts receive) and **best long venue** (lowest rate — longs receive)
3. Calculates:
   - **DPY** = daily % yield (short_rate - long_rate, normalized to daily)
   - **NDPY** = net DPY after deducting round-trip taker fees (entry + exit)
   - **Breakeven hours** = time to recoup taker fees from the daily yield
   - **APY** / **MPY** = annualized and monthly projections
4. Filters for pairs meeting: `NDPY >= ENTRY_NDPY` AND `breakeven <= ENTRY_BREAKEVEN`

## Thresholds (from .env)

- `ENTRY_NDPY` — minimum net daily yield (default 0.0008 = 0.08%)
- `ENTRY_BREAKEVEN` — maximum breakeven hours (default 12)

## How to run

```bash
.venv/bin/python scripts/hedge_scanner.py entry
```

Optional: filter to specific symbols:
```bash
.venv/bin/python scripts/hedge_scanner.py entry --symbols=BTC,ETH,SOL
```

## Presentation

After running, present to the user:

### 1. Rate Matrix
Show funding rates per symbol across all venues (daily rates).

### 2. Qualifying Opportunities
Table of opportunities that pass both NDPY and breakeven thresholds, ranked by DPY:
| # | Symbol | Short@Venue | Long@Venue | DPY | NDPY | Fees | BE(h) | APY | MPY |

### 3. Near Misses
If no opportunities qualify, show the top 5 near-misses and which threshold they fail.

### 4. Actionable Advice
For qualifying opportunities, suggest the trade:
- "Short X on [venue] + Long X on [venue]"
- Size based on account balances (if known from recent /hedgehog query)
- Remind user that all orders will be taker orders

If the user wants to execute, use the appropriate venue market order skills (e.g., /hl-market, /lighter-market).
