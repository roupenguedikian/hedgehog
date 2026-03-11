---
name: hedgehog
description: Query all 6 venues (Hyperliquid, Aster, Lighter, Apex, dYdX, Drift) in parallel for a full portfolio overview. Use when the user wants a cross-venue snapshot of positions, balances, and funding rates.
allowed-tools: Bash, Skill
---

# Hedgehog — Full Portfolio Query

Run all 6 venue query skills in parallel to get a complete cross-venue snapshot.

## How to run

```bash
.venv/bin/python connectors/hedgehog_query.py
```

This runs all 6 venue query scripts in parallel (hl, aster, lighter, apex, dydx, drift) and prints their output sequentially. An optional subcommand (e.g. `funding`, `positions`) is passed through to each venue script.

## After all queries complete

Present a **cross-venue summary** at the end:

### 1. Portfolio Overview Table
| Venue | Equity | Margin Used | Free Margin | uPnL | # Positions |
Show totals row at the bottom.

### 2. Open Positions (all venues)
Consolidated table of all open positions across venues:
| Venue | Symbol | Side | Size (USD) | Entry | uPnL | Leverage |

### 3. Funding Rate Arbitrage Scanner
Compare funding rates for symbols that appear on multiple venues. Highlight spreads > 10% annualized (the min_spread from strategy.yaml). Format:
| Symbol | Venue (highest) | Rate | Venue (lowest) | Rate | Spread (ann.) |

### 4. Key Observations
- Total portfolio value and margin utilization
- Largest positions and their PnL
- Best arb opportunities by spread
- Any positions near liquidation or unusual activity
- If repeated in the same session, note changes from previous query
