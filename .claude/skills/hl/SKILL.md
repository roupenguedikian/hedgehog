---
name: hl
description: Query Hyperliquid account positions, balance, open orders, recent fills, and top funding rates. Use when the user asks about Hyperliquid positions, balance, PnL, account status, or funding rates.
argument-hint: "[address]"
allowed-tools: Bash
---

# Hyperliquid Account & Market Query

Query a Hyperliquid address for positions, balance, open orders, recent fills, and funding rates for the top 20 symbols by volume.

## Address

- If `$ARGUMENTS` is provided and looks like an Ethereum address (starts with `0x`), use that address.
- Otherwise default to: `0x61A585854F78B4547a9b2493aBD3bE4F71d2DFad`

## How to query

Run this command from the project root, substituting `ADDRESS` with the resolved address:

```bash
.venv/bin/python -c "
import asyncio, json
from models.core import VenueConfig, ChainType, VenueTier
from adapters.hyperliquid_adapter import HyperliquidAdapter
import httpx

config = VenueConfig(
    name='Hyperliquid',
    chain='hyperliquid_l1',
    chain_type=ChainType.EVM,
    settlement_chain='hyperliquid_l1',
    funding_cycle_hours=1,
    maker_fee_bps=1.5,
    taker_fee_bps=4.5,
    max_leverage=50,
    collateral_token='USDC',
    api_base_url='https://api.hyperliquid.xyz',
    ws_url='wss://api.hyperliquid.xyz/ws',
    deposit_chain='arbitrum',
    tier=VenueTier.TIER_1,
    zero_gas=True,
    symbol_format='{symbol}',
    symbol_overrides={},
)

adapter = HyperliquidAdapter(config)
ADDRESS = '<ADDRESS>'

async def query():
    await adapter.connect('')
    adapter._address = ADDRESS

    # === BALANCE (raw clearinghouseState for accurate numbers) ===
    raw = await adapter._post_info({'type': 'clearinghouseState', 'user': ADDRESS})
    cross = raw.get('crossMarginSummary', {})
    acct_value = float(cross.get('accountValue', 0))
    total_ntl = float(cross.get('totalNtlPos', 0))
    raw_usd = float(cross.get('totalRawUsd', 0))
    margin_used = float(cross.get('totalMarginUsed', 0))
    withdrawable = float(raw.get('withdrawable', 0))
    maint_margin = float(raw.get('crossMaintenanceMarginUsed', 0))
    free_margin = acct_value - margin_used
    print('=== BALANCE ===')
    print(f'  Account Value (NAV): \${acct_value:,.2f}')
    print(f'  USDC Balance (raw):  \${raw_usd:,.2f}')
    print(f'  Position Notional:   \${total_ntl:,.2f}')
    print(f'  Margin Used:         \${margin_used:,.2f}')
    print(f'  Maint. Margin:       \${maint_margin:,.2f}')
    print(f'  Free Margin:         \${free_margin:,.2f}')
    print(f'  Withdrawable:        \${withdrawable:,.2f}')

    # === POSITIONS ===
    positions = await adapter.get_positions()
    print('\n=== POSITIONS ===')
    if not positions:
        print('  No open positions')
    total_upnl = 0
    for p in positions:
        total_upnl += p.unrealized_pnl
        print(f'  {p.symbol:6s} | {p.side.value.upper():5s} | sz={p.size:>10} | \${p.size_usd:>10,.2f} | entry=\${p.entry_price:,.4f} | uPnL=\${p.unrealized_pnl:>+,.2f} | {p.leverage}x | liq=\${p.liquidation_price:,.4f}')
    print(f'  TOTAL uPnL: \${total_upnl:+,.2f}')

    # === OPEN ORDERS ===
    orders = await adapter._post_info({'type': 'openOrders', 'user': ADDRESS})
    print('\n=== OPEN ORDERS ===')
    if not orders:
        print('  None')
    for o in orders:
        print(f'  {o[\"coin\"]} | {\"BUY\" if o[\"side\"]==\"B\" else \"SELL\"} | px={o[\"limitPx\"]} | sz={o[\"sz\"]}')

    # === LAST 5 FILLS ===
    fills = await adapter._post_info({'type': 'userFills', 'user': ADDRESS})
    print('\n=== LAST 5 FILLS ===')
    if not fills:
        print('  No fills')
    for f in fills[-5:]:
        side = 'BUY' if f['side'] == 'B' else 'SELL'
        print(f'  {f[\"coin\"]:6s} | {side:4s} | px={f[\"px\"]:>10} | sz={f[\"sz\"]:>10} | fee={f.get(\"fee\",\"?\")}')

    # === TOP 20 FUNDING RATES BY VOLUME ===
    meta = await adapter._post_info({'type': 'metaAndAssetCtxs'})
    universe = meta[0]['universe']
    ctxs = meta[1]

    assets = []
    for i, (u, c) in enumerate(zip(universe, ctxs)):
        vol = float(c.get('dayNtlVlm') or 0)
        funding = float(c.get('funding') or 0)
        premium = float(c.get('premium') or 0)
        mark = float(c.get('markPx') or 0)
        oracle = float(c.get('oraclePx') or 0)
        oi = float(c.get('openInterest') or 0)
        ann = funding * 8760
        assets.append({
            'symbol': u['name'],
            'volume_24h': vol,
            'funding': funding,
            'annualized': ann,
            'premium': premium,
            'mark': mark,
            'oracle': oracle,
            'oi': oi * mark,
        })

    top20 = sorted(assets, key=lambda x: x['volume_24h'], reverse=True)[:20]
    print('\n=== TOP 20 FUNDING RATES (by 24h volume) ===')
    print(f'  {\"SYMBOL\":>8s} | {\"RATE/HR\":>10s} | {\"ANNUAL\":>8s} | {\"PREMIUM\":>10s} | {\"MARK\":>12s} | {\"OI (USD)\":>14s} | {\"24H VOL\":>14s}')
    print('  ' + '-' * 90)
    for a in top20:
        print(f'  {a[\"symbol\"]:>8s} | {a[\"funding\"]*100:>9.6f}% | {a[\"annualized\"]*100:>7.2f}% | {a[\"premium\"]*100:>9.6f}% | \${a[\"mark\"]:>11,.2f} | \${a[\"oi\"]:>13,.0f} | \${a[\"volume_24h\"]:>13,.0f}')

    # === EXTREME FUNDING (highest/lowest across all assets) ===
    by_rate = sorted(assets, key=lambda x: x['annualized'], reverse=True)
    print('\n=== EXTREME FUNDING (top 5 highest + top 5 most negative) ===')
    print(f'  {\"SYMBOL\":>8s} | {\"ANNUAL\":>8s} | {\"MARK\":>12s} | {\"OI (USD)\":>14s}')
    print('  ' + '-' * 55)
    print('  -- HIGHEST --')
    for a in by_rate[:5]:
        print(f'  {a[\"symbol\"]:>8s} | {a[\"annualized\"]*100:>+7.2f}% | \${a[\"mark\"]:>11,.4f} | \${a[\"oi\"]:>13,.0f}')
    print('  -- MOST NEGATIVE --')
    for a in by_rate[-5:]:
        print(f'  {a[\"symbol\"]:>8s} | {a[\"annualized\"]*100:>+7.2f}% | \${a[\"mark\"]:>11,.4f} | \${a[\"oi\"]:>13,.0f}')

asyncio.run(query())
"
```

## Output format

Summarize results in clean tables:

1. **Balance** — Account Value (NAV), USDC Balance (raw deposit), Position Notional, Margin Used, Maintenance Margin, Free Margin (NAV - margin), Withdrawable. Note: NAV = raw USDC + unrealized position value, so raw USDC can be negative while NAV is positive
2. **Positions** — symbol, side, size, notional, entry, uPnL, leverage, liquidation price, total uPnL
3. **Open Orders** — symbol, side, price, size (or "None")
4. **Last 5 Fills** — symbol, side, price, size, fee (or "No fills")
5. **Top 20 Funding Rates** — ranked by 24h volume, showing rate/hr, annualized %, premium, mark price, open interest, volume
6. **Extreme Funding** — top 5 highest and top 5 most negative annualized rates across all assets (arb opportunity scanner)

Keep it concise. If the user has been running `/hl` repeatedly in this session, note any changes from the previous query — especially position/balance deltas and any significant funding rate movements.
