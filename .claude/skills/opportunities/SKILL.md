---
name: opportunities
description: Scan all 10 venues for hedge pair opportunities (short+long same symbol) filtered by >$100k volume on both legs, with 1h EMA tracking.
allowed-tools: Bash(python3 *)
argument-hint: "[--min-volume=100000] [--top=50]"
---

# Hedge Opportunities Scanner

Fetch funding rates from all 10 venues (HL, Aster, Lighter, Apex, dYdX, Drift, EdgeX, Paradex, Ethereal, XYZ), find the best hedge pairs per symbol (short highest-rate venue + long lowest-rate venue), require >$100k 24h volume on BOTH legs, and rank by spread APY%.

## How to run

```bash
python3 connectors/opportunities_query.py
```

Optional flags:
- `--min-volume=200000` — override volume filter (default $100k, applied per venue)
- `--top=30` — limit output rows (default 50)

## What it shows

Each row is a hedge pair: short one venue + long another venue for the same symbol.

Columns:
- **SHORT@** / **LONG@** — venue names for each leg
- **S.APY%** / **L.APY%** — annualized funding rate on each leg
- **S.VOL** / **L.VOL** — 24h volume on each leg (both must be >$100k)
- **SPREAD** — gross spread APY% (short_apy - long_apy)
- **EMA** — 1h exponential moving average of the spread
- **DELTA** — current spread minus EMA (highlights regime changes)
- **NET** — spread after round-trip taker fees
- **FEE** — round-trip fee as APY%
- **BE(h)** — breakeven hours to recoup fees from the spread

## Presentation

- Format results in clean markdown tables
- Highlight spreads above strategy min_spread (10% annual from strategy.yaml)
- Note which pairs are net-positive after fees
- The EMA delta flags when a spread is diverging from its smoothed trend
- If repeated in the same session, the EMA becomes more meaningful
