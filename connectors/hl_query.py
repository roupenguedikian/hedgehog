#!/usr/bin/env python3
"""
Hyperliquid account & market query tool — used by the /hl Claude skill.
Usage: python3 connectors/hl_query.py [account|positions|orders|fills|funding|income|all] [address]
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

import httpx

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

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
load_env(os.path.join(_ROOT, ".env"))

API = "https://api.hyperliquid.xyz/info"


async def _post(client: httpx.AsyncClient, payload: dict) -> dict | list:
    resp = await client.post(API, json=payload)
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════
# ACCOUNT
# ═══════════════════════════════════════════════════════════════

async def account(client, address):
    print("=" * 80)
    print("  Hyperliquid — ACCOUNT")
    print("=" * 80)

    raw = await _post(client, {"type": "clearinghouseState", "user": address})
    cross = raw.get("crossMarginSummary", {})
    acct_value = float(cross.get("accountValue", 0))
    total_ntl = float(cross.get("totalNtlPos", 0))
    raw_usd = float(cross.get("totalRawUsd", 0))
    margin_used = float(cross.get("totalMarginUsed", 0))
    withdrawable = float(raw.get("withdrawable", 0))
    maint_margin = float(raw.get("crossMaintenanceMarginUsed", 0))
    free_margin = acct_value - margin_used
    margin_util = (margin_used / acct_value * 100) if acct_value else 0.0

    upnl = 0.0
    for pos in raw.get("assetPositions", []):
        p = pos.get("position", pos)
        upnl += float(p.get("unrealizedPnl", 0))

    print(f"\n  Account Value (NAV): ${acct_value:,.2f}")
    print(f"  USDC Balance (raw):  ${raw_usd:,.2f}")
    print(f"  Position Notional:   ${total_ntl:,.2f}")
    print(f"  Margin Used:         ${margin_used:,.2f}")
    print(f"  Margin Utilization:  {margin_util:.2f}%")
    print(f"  Maint. Margin:       ${maint_margin:,.2f}")
    print(f"  Free Margin:         ${free_margin:,.2f}")
    print(f"  Withdrawable:        ${withdrawable:,.2f}")
    print(f"  Unrealized PnL:      ${upnl:+,.2f}")


# ═══════════════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════════════

async def positions(client, address):
    print("=" * 80)
    print("  Hyperliquid — POSITIONS")
    print("=" * 80)

    raw = await _post(client, {"type": "clearinghouseState", "user": address})
    pos_list = raw.get("assetPositions", [])
    if not pos_list:
        print("\n  No open positions")
        return

    total_upnl = 0
    print(f"\n  {'SYMBOL':>6s} | {'SIDE':>5s} | {'SIZE':>10s} | {'NOTIONAL':>10s} | "
          f"{'ENTRY':>10s} | {'MARK':>10s} | {'uPnL':>10s} | {'LEV':>4s} | {'LIQ':>10s}")
    print("  " + "-" * 105)
    for item in pos_list:
        p = item.get("position", item)
        sz = float(p.get("szi", 0))
        if sz == 0:
            continue
        side = "LONG" if sz > 0 else "SHORT"
        entry = float(p.get("entryPx", 0))
        mark = float(p.get("positionValue", 0)) / abs(sz) if sz else 0
        notional = abs(float(p.get("positionValue", 0)))
        upnl = float(p.get("unrealizedPnl", 0))
        lev = p.get("leverage", {})
        lev_val = lev.get("value", "?") if isinstance(lev, dict) else lev
        liq = float(p.get("liquidationPx", 0) or 0)
        total_upnl += upnl
        print(f"  {p['coin']:>6s} | {side:>5s} | {abs(sz):>10} | "
              f"${notional:>10,.2f} | ${entry:>10,.4f} | "
              f"${mark:>10,.4f} | "
              f"${upnl:>+10,.2f} | {lev_val}x | ${liq:>10,.4f}")
    print(f"  TOTAL uPnL: ${total_upnl:+,.2f}")


# ═══════════════════════════════════════════════════════════════
# OPEN ORDERS
# ═══════════════════════════════════════════════════════════════

async def orders(client, address):
    print("=" * 80)
    print("  Hyperliquid — OPEN ORDERS")
    print("=" * 80)

    ords = await _post(client, {"type": "openOrders", "user": address})
    if not ords:
        print("\n  None")
        return

    print(f"\n  {'SYMBOL':>8s} | {'SIDE':>4s} | {'TYPE':>8s} | {'PRICE':>12s} | "
          f"{'SIZE':>10s} | {'FILLED':>10s} | {'TIF':>5s} | {'STATUS':>6s}")
    print("  " + "-" * 80)
    for o in ords:
        side = "BUY" if o["side"] == "B" else "SELL"
        otype = o.get("orderType", "Limit")
        tif = o.get("tif", "GTC")
        print(f"  {o['coin']:>8s} | {side:>4s} | {otype:>8s} | {o['limitPx']:>12s} | "
              f"{o['sz']:>10s} | {'0':>10s} | {tif:>5s} | {'OPEN':>6s}")


# ═══════════════════════════════════════════════════════════════
# LAST 20 FILLS
# ═══════════════════════════════════════════════════════════════

async def fills(client, address):
    print("=" * 80)
    print("  Hyperliquid — LAST 20 FILLS")
    print("=" * 80)

    fill_list = await _post(client, {"type": "userFills", "user": address})
    if not fill_list:
        print("\n  No fills")
        return

    print(f"\n  {'TIME':>19s} | {'SYMBOL':>6s} | {'SIDE':>4s} | {'PRICE':>12s} | "
          f"{'SIZE':>10s} | {'VALUE':>12s} | {'FEE':>8s} | {'M/T':>3s}")
    print("  " + "-" * 95)
    for f in fill_list[-20:]:
        side = "BUY" if f["side"] == "B" else "SELL"
        ts = datetime.fromtimestamp(int(f['time']) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        value = float(f['px']) * float(f['sz'])
        mt = "T" if f.get("crossed", False) else "M"
        print(f"  {ts:>19s} | {f['coin']:>6s} | {side:>4s} | {f['px']:>12s} | "
              f"{f['sz']:>10s} | ${value:>11,.2f} | {f.get('fee', '?'):>8s} | {mt:>3s}")


# ═══════════════════════════════════════════════════════════════
# FUNDING RATES
# ═══════════════════════════════════════════════════════════════

async def funding(client, address):
    print("=" * 80)
    print("  Hyperliquid — FUNDING RATES (top 20 by 24h volume)")
    print("=" * 80)

    meta = await _post(client, {"type": "metaAndAssetCtxs"})
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
# INCOME (Funding Payments)
# ═══════════════════════════════════════════════════════════════

async def income(client, address):
    print("=" * 80)
    print("  Hyperliquid — INCOME (Funding Payments)")
    print("=" * 80)

    payments = await _post(client, {"type": "userFunding", "user": address})
    if not payments:
        print("\n  No funding payments")
        return

    print(f"\n  {'TIME':>19s} | {'SYMBOL':>8s} | {'RATE':>12s} | {'PAYMENT':>12s}")
    print("  " + "-" * 60)

    net_total = 0.0
    per_symbol = {}
    for entry in payments:
        ts = datetime.fromtimestamp(int(entry['time']) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        delta = entry.get("delta", entry)
        coin = delta["coin"]
        rate = float(delta["fundingRate"])
        payment = float(delta["usdc"])
        net_total += payment
        per_symbol[coin] = per_symbol.get(coin, 0.0) + payment
        print(f"  {ts:>19s} | {coin:>8s} | {rate:>11.8f}% | ${payment:>+11.4f}")

    print("  " + "-" * 60)
    print(f"  NET TOTAL: ${net_total:+,.4f}")
    print(f"\n  Per-symbol breakdown:")
    for sym in sorted(per_symbol, key=lambda s: per_symbol[s], reverse=True):
        print(f"    {sym:>8s}: ${per_symbol[sym]:+,.4f}")


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

SECTIONS = {
    "account": account,
    "positions": positions,
    "orders": orders,
    "fills": fills,
    "funding": funding,
    "income": income,
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

    async with httpx.AsyncClient(timeout=15.0) as client:
        if command == "all":
            await account(client, address)
            print()
            await positions(client, address)
            print()
            await orders(client, address)
            print()
            await fills(client, address)
            print()
            await funding(client, address)
            print()
            await income(client, address)
        elif command in SECTIONS:
            await SECTIONS[command](client, address)
        else:
            print(f"Unknown section: {command}")
            print(f"Valid options: {', '.join(SECTIONS.keys())}, all")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
