#!/usr/bin/env python3
"""
Query trade.xyz (XYZ) HIP-3 DEX on Hyperliquid for funding rates, markets,
account info, positions, orders, fills, and funding payments.

XYZ is a HIP-3 builder DEX on Hyperliquid — all data is accessed via the
Hyperliquid info API with the `dex` parameter set to the XYZ deployer address.
Supports equity perps (stocks), index contracts, and crypto perps.

Usage:
    python3 connectors/tradexyz_query.py [funding|markets|account|positions|orders|fills|income|all] [address]

Environment variables (.env):
    TRADEXYZ_WALLET_ADDRESS    — Wallet address (falls back to HYPERLIQUID_WALLET_ADDRESS)
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

# XYZ HIP-3 dex identifier (short name used in HL API)
XYZ_DEX = "xyz"

# ANSI colors
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
D = "\033[2m"
B = "\033[1m"
X = "\033[0m"


async def _post(client: httpx.AsyncClient, payload: dict) -> dict | list:
    resp = await client.post(API, json=payload)
    resp.raise_for_status()
    return resp.json()


def _clean_symbol(name: str) -> str:
    """Strip 'xyz:' prefix from HIP-3 coin names: 'xyz:NVDA' -> 'NVDA'."""
    if name.startswith("xyz:"):
        return name[4:]
    if ":" in name:
        return name.split(":", 1)[1]
    return name


# ── XYZ universe cache (for filtering orders/fills) ──────────────────
_xyz_coins: set | None = None


async def _get_xyz_universe(client: httpx.AsyncClient) -> tuple[list, list]:
    """Fetch XYZ-specific meta + asset contexts. Returns (universe, ctxs)."""
    global _xyz_coins
    meta = await _post(client, {"type": "metaAndAssetCtxs", "dex": XYZ_DEX})
    universe = meta[0]["universe"]
    ctxs = meta[1]
    # Coin names are 'xyz:NVDA' format
    _xyz_coins = {u["name"] for u in universe}
    return universe, ctxs


# ═══════════════════════════════════════════════════════════════
# PUBLIC — FUNDING RATES
# ═══════════════════════════════════════════════════════════════

async def query_funding(client: httpx.AsyncClient):
    """Funding rates for all XYZ markets + extreme funding scanner."""
    universe, ctxs = await _get_xyz_universe(client)

    print("=" * 82)
    print("  trade.xyz — FUNDING RATES")
    print("=" * 82)

    assets = []
    db_rows = []
    for u, c in zip(universe, ctxs):
        fr = float(c.get("funding") or 0)
        premium = float(c.get("premium") or 0)
        mark = float(c.get("markPx") or 0)
        oracle = float(c.get("oraclePx") or 0)
        oi = float(c.get("openInterest") or 0)
        vol = float(c.get("dayNtlVlm") or 0)
        ann = fr * 8760  # 1h funding cycle
        symbol = _clean_symbol(u["name"])

        assets.append({
            "symbol": symbol, "raw_name": u["name"],
            "funding": fr, "annualized": ann, "premium": premium,
            "mark": mark, "oracle": oracle,
            "oi": oi * mark, "volume_24h": vol,
        })
        db_rows.append({
            "symbol": symbol, "rate": fr, "annualized": ann * 100,
            "cycle_hours": 1, "mark_price": mark, "index_price": oracle,
            "open_interest": oi * mark, "predicted_rate": None,
        })

    by_vol = sorted(assets, key=lambda a: a["volume_24h"], reverse=True)

    print(f"\n  {'SYMBOL':<12} {'RATE/HR':>12} {'ANNUAL':>8} {'PREMIUM':>10} "
          f"{'MARK':>12} {'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 88)
    for a in by_vol:
        if a["volume_24h"] < 100:
            continue
        ann = a["annualized"]
        color = G if ann > 0 else R if ann < 0 else ""
        reset = X if color else ""
        print(f"  {a['symbol']:<12} {a['funding']*100:>11.6f}% "
              f"{color}{ann*100:>+7.2f}%{reset} "
              f"{a['premium']*100:>9.6f}% "
              f"${a['mark']:>11,.2f} ${a['oi']:>13,.0f} ${a['volume_24h']:>13,.0f}")

    # Extreme funding
    by_rate = sorted(assets, key=lambda a: a["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':<12} {'ANNUAL':>8} {'MARK':>12} {'OI (USD)':>14}")
    print("  " + "-" * 52)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:<12} {G}{a['annualized']*100:>+7.2f}%{X} "
              f"${a['mark']:>11,.4f} ${a['oi']:>13,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:<12} {R}{a['annualized']*100:>+7.2f}%{X} "
              f"${a['mark']:>11,.4f} ${a['oi']:>13,.0f}")

    total_vol = sum(a["volume_24h"] for a in assets)
    total_oi = sum(a["oi"] for a in assets)
    avg_rate = (sum(a["annualized"] for a in by_vol[:20]) / min(20, len(by_vol)) * 100) if by_vol else 0
    print(f"\n  Funding cycle: 1h (8760 payments/year)")
    print(f"  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Total open interest: ${total_oi:,.0f}")
    print(f"  Active markets: {len(assets)}")
    print(f"  Avg annualized rate (top 20): {avg_rate:+.2f}%")

    try:
        from db import insert_funding_rates
        await insert_funding_rates("tradexyz", db_rows)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# PUBLIC — MARKETS
# ═══════════════════════════════════════════════════════════════

async def query_markets(client: httpx.AsyncClient):
    """All XYZ markets with price, volume, OI."""
    universe, ctxs = await _get_xyz_universe(client)

    print("=" * 82)
    print("  trade.xyz — ALL MARKETS")
    print("=" * 82)

    assets = []
    for u, c in zip(universe, ctxs):
        mark = float(c.get("markPx") or 0)
        oracle = float(c.get("oraclePx") or 0)
        oi = float(c.get("openInterest") or 0)
        vol = float(c.get("dayNtlVlm") or 0)
        mid = c.get("midPx")
        mid_px = float(mid) if mid else mark
        max_lev = u.get("maxLeverage", "?")

        assets.append({
            "symbol": _clean_symbol(u["name"]),
            "mark": mark, "oracle": oracle,
            "oi": oi * mark, "volume_24h": vol,
            "mid": mid_px, "max_leverage": max_lev,
            "sz_decimals": u.get("szDecimals", "?"),
        })

    assets.sort(key=lambda a: a["volume_24h"], reverse=True)

    print(f"\n  {'SYMBOL':<12} {'MARK':>12} {'ORACLE':>12} "
          f"{'OI (USD)':>14} {'24H VOL':>14} {'MAX LEV':>8}")
    print("  " + "-" * 78)
    for a in assets:
        if a["volume_24h"] < 100:
            continue
        print(f"  {a['symbol']:<12} ${a['mark']:>11,.2f} ${a['oracle']:>11,.2f} "
              f"${a['oi']:>13,.0f} ${a['volume_24h']:>13,.0f} {a['max_leverage']}x")

    print(f"\n  Total markets: {len(assets)}")


# ═══════════════════════════════════════════════════════════════
# PRIVATE — ACCOUNT
# ═══════════════════════════════════════════════════════════════

async def query_account(client: httpx.AsyncClient, address: str):
    """Account balance, equity, margin for XYZ clearinghouse."""
    print("=" * 82)
    print(f"  trade.xyz ACCOUNT — {address[:8]}...{address[-6:]}")
    print("=" * 82)

    try:
        raw = await _post(client, {
            "type": "clearinghouseState", "user": address, "dex": XYZ_DEX,
        })

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
        pos_count = 0
        for pos in raw.get("assetPositions", []):
            p = pos.get("position", pos)
            sz = float(p.get("szi", 0))
            if sz != 0:
                pos_count += 1
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
        print(f"  Open Positions:      {pos_count}")

        try:
            from db import insert_account
            await insert_account("tradexyz", nav=acct_value, wallet_balance=raw_usd,
                                 margin_used=margin_used, free_margin=free_margin,
                                 maint_margin=maint_margin, margin_util_pct=margin_util,
                                 unrealized_pnl=upnl, withdrawable=withdrawable,
                                 position_count=pos_count)
        except Exception:
            pass

    except Exception as ex:
        print(f"\n  {R}Error: {ex}{X}")


# ═══════════════════════════════════════════════════════════════
# PRIVATE — POSITIONS
# ═══════════════════════════════════════════════════════════════

async def query_positions(client: httpx.AsyncClient, address: str):
    """Open positions on XYZ clearinghouse."""
    print("=" * 82)
    print(f"  trade.xyz POSITIONS — {address[:8]}...{address[-6:]}")
    print("=" * 82)

    try:
        raw = await _post(client, {
            "type": "clearinghouseState", "user": address, "dex": XYZ_DEX,
        })

        pos_list = raw.get("assetPositions", [])
        if not pos_list:
            print("\n  No open positions")
            return

        total_upnl = 0.0
        db_rows = []
        print(f"\n  {'SYMBOL':>8s} | {'SIDE':>5s} | {'SIZE':>10s} | {'NOTIONAL':>10s} | "
              f"{'ENTRY':>10s} | {'MARK':>10s} | {'uPnL':>10s} | {'LEV':>4s} | {'LIQ':>10s}")
        print("  " + "-" * 105)

        for item in pos_list:
            p = item.get("position", item)
            sz = float(p.get("szi", 0))
            if sz == 0:
                continue
            side = "LONG" if sz > 0 else "SHORT"
            entry = float(p.get("entryPx", 0))
            notional = abs(float(p.get("positionValue", 0)))
            mark = notional / abs(sz) if sz else 0
            upnl = float(p.get("unrealizedPnl", 0))
            lev = p.get("leverage", {})
            lev_val = lev.get("value", "?") if isinstance(lev, dict) else lev
            liq = float(p.get("liquidationPx", 0) or 0)
            coin = _clean_symbol(p["coin"])
            total_upnl += upnl

            upnl_c = G if upnl > 0 else R if upnl < 0 else ""
            print(f"  {coin:>8s} | {side:>5s} | {abs(sz):>10} | "
                  f"${notional:>10,.2f} | ${entry:>10,.4f} | "
                  f"${mark:>10,.4f} | "
                  f"{upnl_c}${upnl:>+10,.2f}{X} | {lev_val}x | ${liq:>10,.4f}")

            db_rows.append({
                "symbol": coin, "side": side, "size": abs(sz),
                "notional": notional, "entry_price": entry, "mark_price": mark,
                "unrealized_pnl": upnl, "leverage": str(lev_val),
                "liquidation_price": liq,
            })

        print(f"  TOTAL uPnL: ${total_upnl:+,.2f}")

        try:
            from db import insert_positions
            await insert_positions("tradexyz", db_rows)
        except Exception:
            pass

    except Exception as ex:
        print(f"\n  {R}Error: {ex}{X}")


# ═══════════════════════════════════════════════════════════════
# PRIVATE — OPEN ORDERS
# ═══════════════════════════════════════════════════════════════

async def query_orders(client: httpx.AsyncClient, address: str):
    """Active open orders on XYZ markets."""
    if _xyz_coins is None:
        await _get_xyz_universe(client)

    print("=" * 82)
    print(f"  trade.xyz OPEN ORDERS — {address[:8]}...{address[-6:]}")
    print("=" * 82)

    try:
        ords = await _post(client, {"type": "openOrders", "user": address})
        # Filter to XYZ coins only
        xyz_ords = [o for o in ords if o.get("coin", "") in _xyz_coins]

        if not xyz_ords:
            print("\n  No open orders on XYZ markets")
            return

        db_rows = []
        print(f"\n  {'SYMBOL':>8s} | {'SIDE':>4s} | {'TYPE':>8s} | {'PRICE':>12s} | "
              f"{'SIZE':>10s} | {'TIF':>5s}")
        print("  " + "-" * 60)
        for o in xyz_ords:
            coin = _clean_symbol(o["coin"])
            side = "BUY" if o["side"] == "B" else "SELL"
            otype = o.get("orderType", "Limit")
            tif = o.get("tif", "GTC")
            print(f"  {coin:>8s} | {side:>4s} | {otype:>8s} | {o['limitPx']:>12s} | "
                  f"{o['sz']:>10s} | {tif:>5s}")

            db_rows.append({
                "symbol": coin, "side": side, "order_type": otype,
                "price": float(o["limitPx"]), "size": float(o["sz"]),
                "filled": 0, "tif": tif, "status": "OPEN",
                "order_id": str(o.get("oid", "")),
            })

        print(f"\n  Total open orders: {len(xyz_ords)}")

        try:
            from db import insert_orders
            await insert_orders("tradexyz", db_rows)
        except Exception:
            pass

    except Exception as ex:
        print(f"\n  {R}Error: {ex}{X}")


# ═══════════════════════════════════════════════════════════════
# PRIVATE — FILLS
# ═══════════════════════════════════════════════════════════════

async def query_fills(client: httpx.AsyncClient, address: str):
    """Recent trade fills on XYZ markets."""
    if _xyz_coins is None:
        await _get_xyz_universe(client)

    print("=" * 82)
    print(f"  trade.xyz RECENT FILLS — {address[:8]}...{address[-6:]}")
    print("=" * 82)

    try:
        fill_list = await _post(client, {"type": "userFills", "user": address})
        xyz_fills = [f for f in fill_list if f.get("coin", "") in _xyz_coins]

        if not xyz_fills:
            print("\n  No fills on XYZ markets")
            return

        db_rows = []
        print(f"\n  {'TIME':>19s} | {'SYMBOL':>8s} | {'SIDE':>4s} | {'PRICE':>12s} | "
              f"{'SIZE':>10s} | {'VALUE':>12s} | {'FEE':>8s} | {'M/T':>3s}")
        print("  " + "-" * 95)
        for f in xyz_fills[-20:]:
            coin = _clean_symbol(f["coin"])
            side = "BUY" if f["side"] == "B" else "SELL"
            ts = datetime.fromtimestamp(int(f["time"]) / 1000, tz=timezone.utc)
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            value = float(f["px"]) * float(f["sz"])
            mt = "T" if f.get("crossed", False) else "M"
            fee = f.get("fee", "?")
            print(f"  {ts_str:>19s} | {coin:>8s} | {side:>4s} | {f['px']:>12s} | "
                  f"{f['sz']:>10s} | ${value:>11,.2f} | {fee:>8s} | {mt:>3s}")

            db_rows.append({
                "timestamp": ts, "symbol": coin, "side": side,
                "price": float(f["px"]), "size": float(f["sz"]),
                "value": value, "fee": float(fee) if fee != "?" else 0,
                "role": "taker" if mt == "T" else "maker",
            })

        print(f"\n  Total fills shown: {len(xyz_fills[-20:])}")

        try:
            from db import insert_fills
            await insert_fills("tradexyz", db_rows)
        except Exception:
            pass

    except Exception as ex:
        print(f"\n  {R}Error: {ex}{X}")


# ═══════════════════════════════════════════════════════════════
# PRIVATE — INCOME (Funding Payments)
# ═══════════════════════════════════════════════════════════════

async def query_income(client: httpx.AsyncClient, address: str):
    """Funding payment history on XYZ markets."""
    if _xyz_coins is None:
        await _get_xyz_universe(client)

    print("=" * 82)
    print(f"  trade.xyz FUNDING PAYMENTS — {address[:8]}...{address[-6:]}")
    print("=" * 82)

    try:
        payments = await _post(client, {"type": "userFunding", "user": address})
        # Filter to XYZ coins
        xyz_payments = []
        for entry in payments:
            delta = entry.get("delta", entry)
            if delta.get("coin", "") in _xyz_coins:
                xyz_payments.append(entry)

        if not xyz_payments:
            print("\n  No funding payments on XYZ markets")
            return

        db_rows = []
        print(f"\n  {'TIME':>19s} | {'SYMBOL':>8s} | {'RATE':>12s} | {'PAYMENT':>12s}")
        print("  " + "-" * 60)

        net_total = 0.0
        per_symbol = {}
        for entry in xyz_payments:
            ts = datetime.fromtimestamp(int(entry["time"]) / 1000, tz=timezone.utc)
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            delta = entry.get("delta", entry)
            coin = _clean_symbol(delta["coin"])
            rate = float(delta["fundingRate"])
            payment = float(delta["usdc"])
            net_total += payment
            per_symbol[coin] = per_symbol.get(coin, 0.0) + payment

            pmt_c = G if payment > 0 else R if payment < 0 else ""
            print(f"  {ts_str:>19s} | {coin:>8s} | {rate:>11.8f}% | {pmt_c}${payment:>+11.4f}{X}")

            db_rows.append({
                "timestamp": ts, "symbol": coin, "rate": rate, "payment": payment,
            })

        print("  " + "-" * 60)
        total_c = G if net_total > 0 else R if net_total < 0 else ""
        print(f"  NET TOTAL: {total_c}${net_total:+,.4f}{X}")

        if per_symbol:
            print(f"\n  Per-symbol breakdown:")
            for sym in sorted(per_symbol, key=lambda s: per_symbol[s], reverse=True):
                sc = G if per_symbol[sym] > 0 else R if per_symbol[sym] < 0 else ""
                print(f"    {sym:>8s}: {sc}${per_symbol[sym]:+,.4f}{X}")

        try:
            from db import insert_income
            await insert_income("tradexyz", db_rows)
        except Exception:
            pass

    except Exception as ex:
        print(f"\n  {R}Error: {ex}{X}")


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {
    "funding": query_funding,
    "markets": query_markets,
}
ACCT_SECTIONS = {
    "account": query_account,
    "positions": query_positions,
    "orders": query_orders,
    "fills": query_fills,
    "income": query_income,
}
ALL_SECTIONS = {**PUBLIC_SECTIONS, **ACCT_SECTIONS}
ALL_NAMES = list(ALL_SECTIONS.keys()) + ["all"]


async def main():
    args = sys.argv[1:]

    command = "all"
    address = os.environ.get("TRADEXYZ_WALLET_ADDRESS",
              os.environ.get("HYPERLIQUID_WALLET_ADDRESS", ""))

    for arg in args:
        if arg.startswith("0x") and len(arg) > 30:
            address = arg
        elif arg.lower() in ALL_NAMES:
            command = arg.lower()

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    async with httpx.AsyncClient(timeout=15.0) as client:
        if command == "all":
            print(f"\n  trade.xyz Full Query — {ts_str}\n")
            await query_funding(client)
            print()
            await query_markets(client)
            print()
            if address:
                await query_account(client, address)
                print()
                await query_positions(client, address)
                print()
                await query_orders(client, address)
                print()
                await query_fills(client, address)
                print()
                await query_income(client, address)
            else:
                print(f"  {Y}Skipping private endpoints — no wallet address configured{X}")
                print(f"  {Y}Set TRADEXYZ_WALLET_ADDRESS or HYPERLIQUID_WALLET_ADDRESS in .env{X}")
        elif command in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[command](client)
        elif command in ACCT_SECTIONS:
            if not address:
                print(f"Error: No wallet address. Set TRADEXYZ_WALLET_ADDRESS or "
                      f"HYPERLIQUID_WALLET_ADDRESS in .env, or pass an 0x address")
                sys.exit(1)
            await ACCT_SECTIONS[command](client, address)
        else:
            print(f"Unknown command: {command}")
            print(f"Valid: {', '.join(ALL_NAMES)}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
