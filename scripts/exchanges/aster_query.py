#!/usr/bin/env python3
"""
Aster DEX query tool — used by the /aster Claude skill.
Usage: python3 scripts/aster_query.py [funding|account|positions|orders|fills|income|all]
"""
import asyncio
import hashlib
import hmac
import os
import sys
import time
import httpx
from datetime import datetime, timezone
from urllib.parse import urlencode

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

load_env(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))

BASE = "https://fapi.asterdex.com"
API_KEY = os.environ.get("ASTER_API_KEY", "")
API_SECRET = os.environ.get("ASTER_API_SECRET", "")

HAS_AUTH = bool(API_KEY and API_SECRET)

def sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params

def ts(ms):
    if not ms:
        return "N/A"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════
# PUBLIC — no auth needed
# ═══════════════════════════════════════════════════════════════

def _infer_cycle_hours(next_funding_ms: int) -> int:
    """
    Infer funding cycle (1h, 4h, or 8h) from nextFundingTime.
    - 1h-cycle symbols settle on every hour
    - 4h-cycle symbols settle at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00
    - 8h-cycle symbols settle at 00:00, 08:00, 16:00
    We distinguish by checking which schedule the next time fits.
    """
    if not next_funding_ms:
        return 8
    dt = datetime.fromtimestamp(next_funding_ms / 1000, tz=timezone.utc)
    hour = dt.hour
    # 8h: settles at 0, 8, 16 only
    # 4h: settles at 0, 4, 8, 12, 16, 20
    # 1h: settles every hour
    if hour % 8 == 0:
        # Could be 1h, 4h, or 8h — ambiguous, need more info
        # Default to 8h (most common for this alignment)
        return 8
    elif hour % 4 == 0:
        return 4
    else:
        return 1


async def _build_cycle_map(client) -> dict[str, int]:
    """
    Build a map of symbol → cycle_hours by checking funding history
    for symbols whose cycle is ambiguous from nextFundingTime alone.
    Fetches premiumIndex (all symbols) and samples funding history
    for one symbol per distinct nextFundingTime group.
    """
    resp = await client.get("/fapi/v1/premiumIndex")
    resp.raise_for_status()
    all_premium = resp.json()

    # Group symbols by nextFundingTime
    from collections import defaultdict
    nft_groups: dict[int, list[str]] = defaultdict(list)
    for p in all_premium:
        nft = int(p.get("nextFundingTime", 0))
        nft_groups[nft].append(p["symbol"])

    # For each distinct nextFundingTime, sample one symbol's history
    group_cycle: dict[int, int] = {}
    for nft, syms in nft_groups.items():
        try:
            resp2 = await client.get(
                "/fapi/v1/fundingRate",
                params={"symbol": syms[0], "limit": 2},
            )
            resp2.raise_for_status()
            history = resp2.json()
            if len(history) >= 2:
                diff_h = round(
                    (int(history[1]["fundingTime"]) - int(history[0]["fundingTime"]))
                    / (1000 * 3600)
                )
                if diff_h in (1, 4, 8):
                    group_cycle[nft] = diff_h
                else:
                    group_cycle[nft] = 8
            else:
                group_cycle[nft] = _infer_cycle_hours(nft)
        except Exception:
            group_cycle[nft] = _infer_cycle_hours(nft)

    # Build per-symbol map
    cycle_map: dict[str, int] = {}
    for p in all_premium:
        nft = int(p.get("nextFundingTime", 0))
        cycle_map[p["symbol"]] = group_cycle.get(nft, 8)

    return cycle_map


async def funding(client):
    """Top 20 funding rates by volume + extreme rates, with correct per-symbol annualization."""
    print("=" * 80)
    print("  Aster — FUNDING RATES (top 20 by 24h volume)")
    print("=" * 80)

    cycle_map = await _build_cycle_map(client)

    resp = await client.get("/fapi/v1/premiumIndex")
    resp.raise_for_status()
    all_premium = resp.json()

    resp2 = await client.get("/fapi/v1/ticker/24hr")
    resp2.raise_for_status()
    tickers = {t["symbol"]: t for t in resp2.json()}

    assets = []
    for p in all_premium:
        sym = p.get("symbol", "")
        t = tickers.get(sym, {})
        vol = float(t.get("quoteVolume") or 0)
        rate = float(p.get("lastFundingRate") or 0)
        mark = float(p.get("markPrice") or 0)
        index_px = float(p.get("indexPrice") or 0)
        cycle_h = cycle_map.get(sym, 8)
        payments_per_year = 365 * (24 / cycle_h)
        ann = rate * payments_per_year
        chg = float(t.get("priceChangePercent") or 0)
        oi_raw = float(t.get("openInterest") or 0)
        oi_usd = oi_raw * mark if mark > 0 else 0
        assets.append({
            "symbol": sym, "rate": rate, "annualized": ann,
            "mark": mark, "index": index_px, "volume_24h": vol,
            "chg": chg, "cycle_h": cycle_h, "oi_usd": oi_usd,
        })

    top20 = sorted(assets, key=lambda x: x["volume_24h"], reverse=True)[:20]
    print(f"\n  {'SYMBOL':>12s} | {'RATE':>10s} | {'CYCLE':>5s} | {'ANNUAL':>8s} | {'MARK':>12s} | "
          f"{'INDEX':>12s} | {'OI (USD)':>14s} | {'24H VOL':>16s} | {'CHG%':>7s}")
    print("  " + "-" * 121)
    for a in top20:
        print(f"  {a['symbol']:>12s} | {a['rate']*100:>9.4f}% | {a['cycle_h']:>4d}h | {a['annualized']*100:>7.2f}% | "
              f"${a['mark']:>11,.2f} | ${a['index']:>11,.2f} | ${a['oi_usd']:>13,.0f} | "
              f"${a['volume_24h']:>15,.0f} | {a['chg']:>+6.2f}%")

    by_rate = sorted(assets, key=lambda x: x["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':>12s} | {'CYCLE':>5s} | {'ANNUAL':>8s} | {'MARK':>12s} | {'OI (USD)':>14s} | {'24H VOL':>16s}")
    print("  " + "-" * 83)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:>12s} | {a['cycle_h']:>4d}h | {a['annualized']*100:>+7.2f}% | ${a['mark']:>11,.4f} | "
              f"${a['oi_usd']:>13,.0f} | ${a['volume_24h']:>15,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:>12s} | {a['cycle_h']:>4d}h | {a['annualized']*100:>+7.2f}% | ${a['mark']:>11,.4f} | "
              f"${a['oi_usd']:>13,.0f} | ${a['volume_24h']:>15,.0f}")

    # Cycle distribution
    from collections import Counter
    cycle_counts = Counter(a["cycle_h"] for a in assets)
    print(f"\n  Cycle distribution: " + ", ".join(f"{h}h={n}" for h, n in sorted(cycle_counts.items())))

    total_vol = sum(a["volume_24h"] for a in assets)
    active = len([a for a in assets if a["volume_24h"] > 0])
    avg_rate = sum(a["annualized"] for a in top20) / len(top20) if top20 else 0
    print(f"  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Active markets: {active}/{len(assets)}")
    print(f"  Avg annualized rate (top 20): {avg_rate*100:.2f}%")


# ═══════════════════════════════════════════════════════════════
# AUTHENTICATED — requires API key/secret
# ═══════════════════════════════════════════════════════════════

async def account(client):
    """Account balance with margin breakdown."""
    print("=" * 80)
    print("  Aster — ACCOUNT BALANCE")
    print("=" * 80)

    resp = await client.get("/fapi/v4/account", params=sign({}))
    resp.raise_for_status()
    acct = resp.json()

    wallet = float(acct.get("totalWalletBalance") or 0)
    unrealized = float(acct.get("totalUnrealizedProfit") or 0)
    margin_bal = float(acct.get("totalMarginBalance") or 0)
    available = float(acct.get("availableBalance") or 0)
    max_withdraw = float(acct.get("maxWithdrawAmount") or 0)
    pos_margin = float(acct.get("totalPositionInitialMargin") or 0)
    order_margin = float(acct.get("totalOpenOrderInitialMargin") or 0)
    maint_margin = float(acct.get("totalMaintMargin") or 0)
    margin_used = pos_margin + order_margin

    margin_util = margin_used / margin_bal * 100 if margin_bal > 0 else 0

    print(f"\n  NAV (Equity):         ${margin_bal:,.2f}")
    print(f"  Wallet Balance:       ${wallet:,.2f}")
    print(f"  Unrealized PnL:       ${unrealized:+,.2f}")
    print(f"  Margin Balance:       ${margin_bal:,.2f}")
    print(f"  Position Margin:      ${pos_margin:,.2f}")
    print(f"  Order Margin:         ${order_margin:,.2f}")
    print(f"  Margin Used (total):  ${margin_used:,.2f} ({margin_util:.1f}%)")
    print(f"  Maint. Margin:        ${maint_margin:,.2f}")
    print(f"  Free Margin:          ${available:,.2f}")
    print(f"  Max Withdraw:         ${max_withdraw:,.2f}")
    print(f"  Fee Tier:             {acct.get('feeTier', '?')}")

    # Per-asset balances
    assets_with_bal = [a for a in acct.get("assets", [])
                       if float(a.get("walletBalance", 0)) != 0]
    if assets_with_bal:
        print(f"\n  Asset Balances:")
        for a in assets_with_bal:
            print(f"    {a['asset']:>6s}: wallet=${float(a.get('walletBalance',0)):,.2f}  "
                  f"available=${float(a.get('availableBalance',0)):,.2f}  "
                  f"uPnL=${float(a.get('unrealizedProfit',0)):+,.2f}")


async def positions(client):
    """Open positions."""
    print("=" * 80)
    print("  Aster — POSITIONS")
    print("=" * 80)

    resp = await client.get("/fapi/v4/account", params=sign({}))
    resp.raise_for_status()
    acct = resp.json()

    open_pos = [p for p in acct.get("positions", [])
                if float(p.get("positionAmt", 0)) != 0]

    if not open_pos:
        print("\n  No open positions")
        return

    total_upnl = 0
    print(f"\n  {'SYMBOL':>12s} | {'SIDE':>5s} | {'SIZE':>10s} | {'NOTIONAL':>12s} | "
          f"{'ENTRY':>10s} | {'MARK':>10s} | {'uPnL':>10s} | {'LEV':>5s} | {'LIQ':>10s}")
    print("  " + "-" * 105)
    for p in open_pos:
        amt = float(p.get("positionAmt", 0))
        side = "LONG" if amt > 0 else "SHORT"
        notional = abs(float(p.get("notional", 0)))
        entry = float(p.get("entryPrice", 0))
        mark = float(p.get("markPrice", 0))
        upnl = float(p.get("unrealizedProfit", 0))
        liq = float(p.get("liquidationPrice", 0))
        lev = p.get("leverage", "?")
        total_upnl += upnl
        print(f"  {p['symbol']:>12s} | {side:>5s} | {abs(amt):>10.4f} | ${notional:>11,.2f} | "
              f"${entry:>9,.4f} | ${mark:>9,.4f} | ${upnl:>+9,.2f} | {lev:>5s}x | ${liq:>9,.4f}")
    print(f"  TOTAL uPnL: ${total_upnl:+,.2f}")


async def orders(client):
    """Open orders."""
    print("=" * 80)
    print("  Aster — OPEN ORDERS")
    print("=" * 80)

    resp = await client.get("/fapi/v1/openOrders", params=sign({}))
    resp.raise_for_status()
    ords = resp.json()

    if not ords:
        print("\n  None")
        return

    print(f"\n  {'SYMBOL':>12s} | {'SIDE':>5s} | {'TYPE':>8s} | {'PRICE':>12s} | "
          f"{'QTY':>10s} | {'FILLED':>10s} | {'STATUS':>10s} | {'TIF':>5s} | {'TIME':>20s}")
    print("  " + "-" * 113)
    for o in ords:
        print(f"  {o['symbol']:>12s} | {o.get('side',''):>5s} | {o.get('type',''):>8s} | "
              f"${float(o.get('price',0)):>11,.4f} | {float(o.get('origQty',0)):>10.4f} | "
              f"{float(o.get('executedQty',0)):>10.4f} | {o.get('status',''):>10s} | "
              f"{o.get('timeInForce',''):>5s} | {ts(o.get('time', 0)):>20s}")


async def fills(client):
    """Recent trade history (last 20 fills)."""
    print("=" * 80)
    print("  Aster — RECENT FILLS (last 20)")
    print("=" * 80)

    resp = await client.get("/fapi/v1/userTrades", params=sign({"limit": 20}))
    resp.raise_for_status()
    trades = resp.json()

    if not trades:
        print("\n  No recent fills")
        return

    total_fee = 0
    print(f"\n  {'TIME':>20s} | {'SYMBOL':>12s} | {'SIDE':>5s} | {'PRICE':>12s} | "
          f"{'QTY':>10s} | {'QUOTE':>12s} | {'FEE':>10s} | {'MAKER':>5s}")
    print("  " + "-" * 105)
    for t in trades:
        fee = float(t.get("commission", 0))
        total_fee += fee
        print(f"  {ts(t.get('time',0)):>20s} | {t.get('symbol',''):>12s} | "
              f"{t.get('side',''):>5s} | ${float(t.get('price',0)):>11,.4f} | "
              f"{float(t.get('qty',0)):>10.4f} | ${float(t.get('quoteQty',0)):>11,.2f} | "
              f"${fee:>9,.4f} | {'Y' if t.get('maker') else 'N':>5s}")
    print(f"  Total fees: ${total_fee:,.4f}")


async def income(client):
    """Recent income history (funding fees, PnL, commissions)."""
    print("=" * 80)
    print("  Aster — INCOME HISTORY (last 30)")
    print("=" * 80)

    resp = await client.get("/fapi/v1/income", params=sign({"limit": 30}))
    resp.raise_for_status()
    inc = resp.json()

    if not inc:
        print("\n  No income entries")
        return

    # Split out funding payments
    funding_entries = [i for i in inc if i.get("incomeType") == "FUNDING_FEE"]
    other_entries = [i for i in inc if i.get("incomeType") != "FUNDING_FEE"]

    if funding_entries:
        print(f"\n  FUNDING PAYMENTS:")
        print(f"  {'TIME':>20s} | {'SYMBOL':>12s} | {'AMOUNT':>12s}")
        print("  " + "-" * 50)
        total_funding = 0
        for i in funding_entries:
            amt = float(i.get("income", 0))
            total_funding += amt
            print(f"  {ts(i.get('time',0)):>20s} | {i.get('symbol',''):>12s} | ${amt:>+11,.4f}")
        print(f"  Net funding: ${total_funding:+,.4f}")

    if other_entries:
        print(f"\n  OTHER INCOME:")
        print(f"  {'TIME':>20s} | {'TYPE':>20s} | {'SYMBOL':>12s} | {'AMOUNT':>12s}")
        print("  " + "-" * 75)
        for i in other_entries:
            print(f"  {ts(i.get('time',0)):>20s} | {i.get('incomeType',''):>20s} | "
                  f"{i.get('symbol',''):>12s} | ${float(i.get('income',0)):>+11,.4f}")

    # Totals by type
    totals = {}
    for i in inc:
        t = i.get("incomeType", "UNKNOWN")
        totals[t] = totals.get(t, 0) + float(i.get("income", 0))
    print(f"\n  Totals by type:")
    for t, v in sorted(totals.items()):
        print(f"    {t:>24s}: ${v:+,.4f}")


# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {"funding": funding}
AUTH_SECTIONS = {
    "account": account,
    "positions": positions,
    "orders": orders,
    "fills": fills,
    "income": income,
}
ALL_SECTIONS = {**PUBLIC_SECTIONS, **AUTH_SECTIONS}

async def main():
    arg = sys.argv[1].lower().strip() if len(sys.argv) > 1 else "all"

    headers = {"Content-Type": "application/json"}
    if HAS_AUTH:
        headers["X-MBX-APIKEY"] = API_KEY

    async with httpx.AsyncClient(base_url=BASE, timeout=15.0, headers=headers) as client:
        if arg == "all":
            # Always run funding (public)
            await funding(client)
            print()
            # Run auth sections if keys available
            if HAS_AUTH:
                for name, fn in AUTH_SECTIONS.items():
                    try:
                        await fn(client)
                    except httpx.HTTPStatusError as e:
                        print(f"\n  {name}: HTTP {e.response.status_code} — {e.response.text[:100]}")
                    print()
            else:
                print("  [Auth sections skipped — set ASTER_API_KEY and ASTER_API_SECRET in .env]")
        elif arg in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[arg](client)
        elif arg in AUTH_SECTIONS:
            if not HAS_AUTH:
                print(f"ERROR: {arg} requires ASTER_API_KEY and ASTER_API_SECRET in .env")
                sys.exit(1)
            await AUTH_SECTIONS[arg](client)
        else:
            print(f"Unknown section: {arg}")
            print(f"Valid options: {', '.join(ALL_SECTIONS.keys())}, all")
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
