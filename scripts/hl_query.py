#!/usr/bin/env python3
"""
Hyperliquid account & market query tool — used by the /hl Claude skill.
Usage: python3 scripts/hl_query.py [balance|positions|orders|fills|funding|all] [address]
"""
import asyncio
import os
import sys

# ── Load .env ────────────────────────────────────────────────────────
def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.core import VenueConfig, ChainType, VenueTier
from adapters.hyperliquid_adapter import HyperliquidAdapter


def _make_config():
    return VenueConfig(
        name="Hyperliquid",
        chain="hyperliquid_l1",
        chain_type=ChainType.EVM,
        settlement_chain="hyperliquid_l1",
        funding_cycle_hours=1,
        maker_fee_bps=1.5,
        taker_fee_bps=4.5,
        max_leverage=50,
        collateral_token="USDC",
        api_base_url="https://api.hyperliquid.xyz",
        ws_url="wss://api.hyperliquid.xyz/ws",
        deposit_chain="arbitrum",
        tier=VenueTier.TIER_1,
        zero_gas=True,
        symbol_format="{symbol}",
        symbol_overrides={},
    )


async def _connect(address: str) -> HyperliquidAdapter:
    config = _make_config()
    adapter = HyperliquidAdapter(config)
    await adapter.connect("")
    adapter._address = address
    return adapter


# ═══════════════════════════════════════════════════════════════
# BALANCE
# ═══════════════════════════════════════════════════════════════

async def balance(adapter, address):
    print("=" * 80)
    print("  Hyperliquid — BALANCE")
    print("=" * 80)

    raw = await adapter._post_info({"type": "clearinghouseState", "user": address})
    cross = raw.get("crossMarginSummary", {})
    acct_value = float(cross.get("accountValue", 0))
    total_ntl = float(cross.get("totalNtlPos", 0))
    raw_usd = float(cross.get("totalRawUsd", 0))
    margin_used = float(cross.get("totalMarginUsed", 0))
    withdrawable = float(raw.get("withdrawable", 0))
    maint_margin = float(raw.get("crossMaintenanceMarginUsed", 0))
    free_margin = acct_value - margin_used

    print(f"\n  Account Value (NAV): ${acct_value:,.2f}")
    print(f"  USDC Balance (raw):  ${raw_usd:,.2f}")
    print(f"  Position Notional:   ${total_ntl:,.2f}")
    print(f"  Margin Used:         ${margin_used:,.2f}")
    print(f"  Maint. Margin:       ${maint_margin:,.2f}")
    print(f"  Free Margin:         ${free_margin:,.2f}")
    print(f"  Withdrawable:        ${withdrawable:,.2f}")


# ═══════════════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════════════

async def positions(adapter, address):
    print("=" * 80)
    print("  Hyperliquid — POSITIONS")
    print("=" * 80)

    pos_list = await adapter.get_positions()
    if not pos_list:
        print("\n  No open positions")
        return

    total_upnl = 0
    print(f"\n  {'SYMBOL':>6s} | {'SIDE':>5s} | {'SIZE':>10s} | {'NOTIONAL':>10s} | "
          f"{'ENTRY':>10s} | {'uPnL':>10s} | {'LEV':>4s} | {'LIQ':>10s}")
    print("  " + "-" * 85)
    for p in pos_list:
        total_upnl += p.unrealized_pnl
        print(f"  {p.symbol:>6s} | {p.side.value.upper():>5s} | sz={p.size:>10} | "
              f"${p.size_usd:>10,.2f} | entry=${p.entry_price:,.4f} | "
              f"uPnL=${p.unrealized_pnl:>+,.2f} | {p.leverage}x | liq=${p.liquidation_price:,.4f}")
    print(f"  TOTAL uPnL: ${total_upnl:+,.2f}")


# ═══════════════════════════════════════════════════════════════
# OPEN ORDERS
# ═══════════════════════════════════════════════════════════════

async def orders(adapter, address):
    print("=" * 80)
    print("  Hyperliquid — OPEN ORDERS")
    print("=" * 80)

    ords = await adapter._post_info({"type": "openOrders", "user": address})
    if not ords:
        print("\n  None")
        return

    for o in ords:
        side = "BUY" if o["side"] == "B" else "SELL"
        print(f"  {o['coin']} | {side} | px={o['limitPx']} | sz={o['sz']}")


# ═══════════════════════════════════════════════════════════════
# LAST 5 FILLS
# ═══════════════════════════════════════════════════════════════

async def fills(adapter, address):
    print("=" * 80)
    print("  Hyperliquid — LAST 5 FILLS")
    print("=" * 80)

    fill_list = await adapter._post_info({"type": "userFills", "user": address})
    if not fill_list:
        print("\n  No fills")
        return

    for f in fill_list[-5:]:
        side = "BUY" if f["side"] == "B" else "SELL"
        print(f"  {f['coin']:6s} | {side:4s} | px={f['px']:>10} | sz={f['sz']:>10} | fee={f.get('fee', '?')}")


# ═══════════════════════════════════════════════════════════════
# FUNDING RATES
# ═══════════════════════════════════════════════════════════════

async def funding(adapter, address):
    print("=" * 80)
    print("  Hyperliquid — FUNDING RATES (top 20 by 24h volume)")
    print("=" * 80)

    meta = await adapter._post_info({"type": "metaAndAssetCtxs"})
    universe = meta[0]["universe"]
    ctxs = meta[1]

    assets = []
    for u, c in zip(universe, ctxs):
        vol = float(c.get("dayNtlVlm") or 0)
        fr = float(c.get("funding") or 0)
        premium = float(c.get("premium") or 0)
        mark = float(c.get("markPx") or 0)
        oracle = float(c.get("oraclePx") or 0)
        oi = float(c.get("openInterest") or 0)
        ann = fr * 8760
        assets.append({
            "symbol": u["name"], "volume_24h": vol, "funding": fr,
            "annualized": ann, "premium": premium, "mark": mark,
            "oracle": oracle, "oi": oi * mark,
        })

    top20 = sorted(assets, key=lambda x: x["volume_24h"], reverse=True)[:20]
    print(f"\n  {'SYMBOL':>8s} | {'RATE/HR':>10s} | {'ANNUAL':>8s} | {'PREMIUM':>10s} | "
          f"{'MARK':>12s} | {'OI (USD)':>14s} | {'24H VOL':>14s}")
    print("  " + "-" * 90)
    for a in top20:
        print(f"  {a['symbol']:>8s} | {a['funding']*100:>9.6f}% | {a['annualized']*100:>7.2f}% | "
              f"{a['premium']*100:>9.6f}% | ${a['mark']:>11,.2f} | ${a['oi']:>13,.0f} | "
              f"${a['volume_24h']:>13,.0f}")

    # Extreme funding
    by_rate = sorted(assets, key=lambda x: x["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':>8s} | {'ANNUAL':>8s} | {'MARK':>12s} | {'OI (USD)':>14s}")
    print("  " + "-" * 55)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:>8s} | {a['annualized']*100:>+7.2f}% | ${a['mark']:>11,.4f} | ${a['oi']:>13,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:>8s} | {a['annualized']*100:>+7.2f}% | ${a['mark']:>11,.4f} | ${a['oi']:>13,.0f}")


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

SECTIONS = {
    "balance": balance,
    "positions": positions,
    "orders": orders,
    "fills": fills,
    "funding": funding,
}


async def main():
    args = sys.argv[1:]

    command = "all"
    address = os.environ.get("HYPERLIQUID_WALLET_ADDRESS", "")

    for arg in args:
        if arg.startswith("0x") and len(arg) > 30:
            address = arg
        elif arg.lower() in list(SECTIONS) + ["all"]:
            command = arg.lower()

    if not address:
        print("ERROR: HYPERLIQUID_WALLET_ADDRESS not set in .env and no address provided")
        sys.exit(1)

    adapter = await _connect(address)

    if command == "all":
        await balance(adapter, address)
        print()
        await positions(adapter, address)
        print()
        await orders(adapter, address)
        print()
        await fills(adapter, address)
        print()
        await funding(adapter, address)
    elif command in SECTIONS:
        await SECTIONS[command](adapter, address)
    else:
        print(f"Unknown section: {command}")
        print(f"Valid options: {', '.join(SECTIONS.keys())}, all")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
