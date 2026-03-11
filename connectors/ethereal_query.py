#!/usr/bin/env python3
"""
Ethereal DEX query tool — used by the /ethereal Claude skill.
Usage: python3 connectors/ethereal_query.py [funding|account|positions|orders|fills|income|all] [address]
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

load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE = "https://api.ethereal.trade"
DEFAULT_ADDRESS = os.getenv("ETHEREAL_WALLET_ADDRESS", "")


def ts(ms):
    if not ms:
        return "N/A"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def paginate(client, path, params=None, limit=100):
    """Fetch all pages from a paginated Ethereal endpoint."""
    params = dict(params or {})
    params.setdefault("limit", limit)
    results = []
    while True:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        body = resp.json()
        results.extend(body.get("data", []))
        if not body.get("hasNext"):
            break
        params["cursor"] = body["nextCursor"]
    return results


# ═══════════════════════════════════════════════════════════════
# RESOLVE — wallet address → subaccount ID + product map
# ═══════════════════════════════════════════════════════════════

async def resolve_subaccount(client, address):
    """Resolve wallet address to first subaccount ID."""
    resp = await client.get("/v1/subaccount", params={"sender": address, "limit": 10})
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if data:
        return data[0]["id"]
    return ""


async def get_product_map(client):
    """Build ticker → product dict for all products."""
    products = await paginate(client, "/v1/product", {"limit": 100})
    return {p["displayTicker"]: p for p in products}


# ═══════════════════════════════════════════════════════════════
# PUBLIC — no auth needed
# ═══════════════════════════════════════════════════════════════

async def funding(client):
    """All products with funding rates + extreme scanner + projected rates."""
    print("=" * 80)
    print("  Ethereal — FUNDING RATES (all products)")
    print("=" * 80)

    products = await paginate(client, "/v1/product", {"limit": 100})
    product_ids = [p["id"] for p in products if p.get("id")]

    # Fetch oracle/mark prices for all products
    prices = {}
    if product_ids:
        try:
            resp = await client.get("/v1/product/market-price",
                                    params={"productIds": product_ids[:50]})
            resp.raise_for_status()
            for mp in resp.json().get("data", []):
                prices[mp["productId"]] = {
                    "oracle": float(mp.get("oraclePrice") or 0),
                    "bid": float(mp.get("bestBidPrice") or 0),
                    "ask": float(mp.get("bestAskPrice") or 0),
                }
        except Exception:
            pass

    rows = []
    for p in products:
        pid = p.get("id", "")
        ticker = p.get("displayTicker", p.get("ticker", "?"))
        rate_1h = float(p.get("fundingRate1h") or 0)
        oi_base = float(p.get("openInterest") or 0)
        vol_base = float(p.get("volume24h") or 0)
        # fundingRate1h is per-hour decimal, annualize: rate * 8760
        ann = rate_1h * 8760
        px = prices.get(pid, {})
        oracle = px.get("oracle", 0)
        mid = (px.get("bid", 0) + px.get("ask", 0)) / 2 if px.get("bid") else oracle
        mark = mid if mid > 0 else oracle
        oi_usd = oi_base * oracle if oracle > 0 else 0
        vol_usd = vol_base * oracle if oracle > 0 else 0
        rows.append({
            "ticker": ticker, "rate_1h": rate_1h, "annualized": ann,
            "mark": mark, "oracle": oracle, "oi_usd": oi_usd,
            "volume_24h": vol_usd, "product_id": pid,
        })

    # Sort by volume
    by_vol = sorted(rows, key=lambda x: x["volume_24h"], reverse=True)
    print(f"\n  {'SYMBOL':>12s} | {'RATE/1H':>10s} | {'ANNUAL':>8s} | {'MARK':>12s} | "
          f"{'ORACLE':>12s} | {'OI (USD)':>14s} | {'24H VOL':>16s}")
    print("  " + "-" * 101)
    for a in by_vol[:20]:
        print(f"  {a['ticker']:>12s} | {a['rate_1h']*100:>9.4f}% | {a['annualized']*100:>+7.2f}% | "
              f"${a['mark']:>11,.2f} | ${a['oracle']:>11,.2f} | ${a['oi_usd']:>13,.0f} | "
              f"${a['volume_24h']:>15,.0f}")

    # Extreme funding
    by_rate = sorted(rows, key=lambda x: x["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':>12s} | {'ANNUAL':>8s} | {'ORACLE':>12s} | {'OI (USD)':>14s} | {'24H VOL':>16s}")
    print("  " + "-" * 71)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['ticker']:>12s} | {a['annualized']*100:>+7.2f}% | ${a['oracle']:>11,.4f} | "
              f"${a['oi_usd']:>13,.0f} | ${a['volume_24h']:>15,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['ticker']:>12s} | {a['annualized']*100:>+7.2f}% | ${a['oracle']:>11,.4f} | "
              f"${a['oi_usd']:>13,.0f} | ${a['volume_24h']:>15,.0f}")

    # Projected rates
    if product_ids:
        try:
            resp = await client.get("/v1/funding/projected-rate",
                                    params={"productIds": product_ids[:50]})
            resp.raise_for_status()
            proj_data = resp.json().get("data", [])
            if proj_data:
                id_to_ticker = {r["product_id"]: r["ticker"] for r in rows}
                print(f"\n  PROJECTED RATES:")
                print(f"  {'SYMBOL':>12s} | {'CURRENT/1H':>12s} | {'PROJECTED/1H':>14s} | {'PROJ ANN':>10s}")
                print("  " + "-" * 58)
                for pf in proj_data:
                    pid = pf.get("productId", "")
                    ticker = id_to_ticker.get(pid, "?")
                    current = float(pf.get("fundingRate1h") or 0)
                    projected = float(pf.get("fundingRateProjected1h") or 0)
                    proj_ann = projected * 8760 * 100
                    print(f"  {ticker:>12s} | {current*100:>11.4f}% | {projected*100:>13.4f}% | {proj_ann:>+9.2f}%")
        except Exception:
            pass

    total_vol = sum(a["volume_24h"] for a in rows)
    total_oi = sum(a["oi_usd"] for a in rows)
    avg_rate = sum(a["annualized"] for a in by_vol[:20]) / len(by_vol[:20]) if by_vol else 0
    print(f"\n  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Total open interest: ${total_oi:,.0f}")
    print(f"  Active markets: {len(rows)}")
    print(f"  Avg annualized rate (top 20): {avg_rate*100:+.2f}%")


# ═══════════════════════════════════════════════════════════════
# ACCOUNT — requires wallet address
# ═══════════════════════════════════════════════════════════════

async def account(client, address):
    """Subaccount balances and margin."""
    print("=" * 80)
    print(f"  Ethereal — ACCOUNT: {address}")
    print("=" * 80)

    resp = await client.get("/v1/subaccount", params={"sender": address, "limit": 10})
    resp.raise_for_status()
    subaccounts = resp.json().get("data", [])

    if not subaccounts:
        print("\n  No subaccounts found for this address")
        return

    for sa in subaccounts:
        sa_id = sa["id"]
        sa_name = sa.get("name", "default")
        print(f"\n  -- Subaccount: {sa_id[:8]}... (name: {sa_name})")

        # Balances
        bal_resp = await client.get("/v1/subaccount/balance",
                                    params={"subaccountId": sa_id, "limit": 20})
        bal_resp.raise_for_status()
        balances = bal_resp.json().get("data", [])

        total_balance = 0
        total_available = 0
        for b in balances:
            amount = float(b.get("amount") or 0)
            available = float(b.get("available") or 0)
            token = b.get("tokenName", "?")
            total_balance += amount
            total_available += available
            print(f"     {token}: balance=${amount:,.2f}  available=${available:,.2f}")

        margin_used = total_balance - total_available
        margin_util = (margin_used / total_balance * 100) if total_balance > 0 else 0

        print(f"\n     NAV (Equity):         ${total_balance:,.2f}")
        print(f"     Margin Used:          ${margin_used:,.2f} ({margin_util:.1f}%)")
        print(f"     Free Margin:          ${total_available:,.2f}")


async def positions(client, address):
    """Open positions."""
    print("=" * 80)
    print(f"  Ethereal — POSITIONS: {address}")
    print("=" * 80)

    sa_id = await resolve_subaccount(client, address)
    if not sa_id:
        print("\n  No subaccounts found")
        return

    prod_map = await get_product_map(client)
    id_to_ticker = {p["id"]: p["displayTicker"] for p in prod_map.values()}

    pos_list = await paginate(client, "/v1/position",
                              {"subaccountId": sa_id, "open": "true", "limit": 50})

    if not pos_list:
        print("\n  No open positions")
        return

    total_upnl = 0
    print(f"\n  {'SYMBOL':>12s} | {'SIDE':>5s} | {'SIZE':>10s} | {'NOTIONAL':>12s} | "
          f"{'ENTRY':>10s} | {'MARK':>10s} | {'uPnL':>10s} | {'LEV':>5s} | {'LIQ':>10s}")
    print("  " + "-" * 105)
    for p in pos_list:
        size = float(p.get("size") or 0)
        if size == 0:
            continue
        side_num = p.get("side", 0)
        side = "LONG" if side_num == 0 else "SHORT"
        entry = float(p.get("entryPrice") or 0)
        mark = float(p.get("markPrice") or 0)
        pnl = float(p.get("pnl") or 0)
        lev = p.get("leverage", "?")
        liq = float(p.get("liquidationPrice") or 0)
        margin = float(p.get("margin") or 0)
        ticker = id_to_ticker.get(p.get("productId", ""), "?")
        notional = size * mark
        total_upnl += pnl
        print(f"  {ticker:>12s} | {side:>5s} | {size:>10.4f} | ${notional:>11,.2f} | "
              f"${entry:>9,.4f} | ${mark:>9,.4f} | ${pnl:>+9,.2f} | {str(lev):>5s}x | "
              + (f"${liq:>9,.4f}" if liq > 0 else f"{'N/A':>10s}"))
    print(f"  TOTAL uPnL: ${total_upnl:+,.2f}")


async def orders(client, address):
    """Open orders."""
    print("=" * 80)
    print(f"  Ethereal — OPEN ORDERS: {address}")
    print("=" * 80)

    sa_id = await resolve_subaccount(client, address)
    if not sa_id:
        print("\n  No subaccounts found")
        return

    prod_map = await get_product_map(client)
    id_to_ticker = {p["id"]: p["displayTicker"] for p in prod_map.values()}

    order_list = await paginate(client, "/v1/order",
                                {"subaccountId": sa_id, "isWorking": "true", "limit": 50})

    if not order_list:
        print("\n  No open orders")
        return

    print(f"\n  {'SYMBOL':>12s} | {'SIDE':>5s} | {'TYPE':>8s} | {'PRICE':>12s} | "
          f"{'QTY':>10s} | {'FILLED':>10s} | {'STATUS':>10s} | {'TIF':>5s} | {'TIME':>20s}")
    print("  " + "-" * 113)
    for o in order_list:
        side_num = o.get("side", 0)
        side = "BUY" if side_num == 0 else "SELL"
        ticker = id_to_ticker.get(o.get("productId", ""), "?")
        price = float(o.get("price") or 0)
        qty = float(o.get("quantity") or 0)
        filled = float(o.get("filled") or 0)
        status = o.get("status", "?")
        tif = o.get("timeInForce", "?")
        otype = o.get("type", "?")
        created = ts(o.get("createdAt", 0))
        print(f"  {ticker:>12s} | {side:>5s} | {otype:>8s} | "
              f"${price:>11,.4f} | {qty:>10.4f} | "
              f"{filled:>10.4f} | {status:>10s} | "
              f"{tif:>5s} | {created:>20s}")


async def fills(client, address):
    """Recent trade fills."""
    print("=" * 80)
    print(f"  Ethereal — RECENT FILLS (last 20): {address}")
    print("=" * 80)

    sa_id = await resolve_subaccount(client, address)
    if not sa_id:
        print("\n  No subaccounts found")
        return

    prod_map = await get_product_map(client)
    id_to_ticker = {p["id"]: p["displayTicker"] for p in prod_map.values()}

    resp = await client.get("/v1/order/fill",
                            params={"subaccountId": sa_id, "limit": 20, "order": "desc"})
    resp.raise_for_status()
    fill_list = resp.json().get("data", [])

    if not fill_list:
        print("\n  No recent fills")
        return

    total_fee = 0
    print(f"\n  {'TIME':>20s} | {'SYMBOL':>12s} | {'SIDE':>5s} | {'PRICE':>12s} | "
          f"{'QTY':>10s} | {'QUOTE':>12s} | {'FEE':>10s}")
    print("  " + "-" * 95)
    for f in fill_list:
        side_num = f.get("side", 0)
        side = "BUY" if side_num == 0 else "SELL"
        ticker = id_to_ticker.get(f.get("productId", ""), "?")
        price = float(f.get("price") or 0)
        filled = float(f.get("filled") or 0)
        fee = float(f.get("fee") or 0)
        quote = price * filled
        total_fee += fee
        created = ts(f.get("createdAt", 0))
        print(f"  {created:>20s} | {ticker:>12s} | {side:>5s} | "
              f"${price:>11,.4f} | {filled:>10.4f} | "
              f"${quote:>11,.2f} | ${fee:>9,.4f}")
    print(f"  Total fees: ${total_fee:,.4f}")


async def income(client, address):
    """Funding rate history for held products (last day)."""
    print("=" * 80)
    print(f"  Ethereal — FUNDING HISTORY (last day): {address}")
    print("=" * 80)

    sa_id = await resolve_subaccount(client, address)
    if not sa_id:
        print("\n  No subaccounts found")
        return

    prod_map = await get_product_map(client)
    id_to_ticker = {p["id"]: p["displayTicker"] for p in prod_map.values()}

    # Get open positions to know which products to query
    pos_list = await paginate(client, "/v1/position",
                              {"subaccountId": sa_id, "open": "true", "limit": 50})

    if not pos_list:
        print("\n  No open positions — no funding to show")
        return

    # For each position's product, fetch recent funding history
    total_funding_unrealized = 0
    for p in pos_list:
        pid = p.get("productId", "")
        ticker = id_to_ticker.get(pid, "?")
        size = float(p.get("size") or 0)
        side_num = p.get("side", 0)
        side = "LONG" if side_num == 0 else "SHORT"
        funding_unrealized = float(p.get("fundingUnrealized") or 0)
        total_funding_unrealized += funding_unrealized

        if not pid:
            continue

        try:
            resp = await client.get("/v1/funding",
                                    params={"productId": pid, "range": "DAY",
                                            "limit": 24, "order": "desc"})
            resp.raise_for_status()
            entries = resp.json().get("data", [])
            if entries:
                print(f"\n  {ticker} ({side} {size:.4f})  unrealized funding: ${funding_unrealized:+,.4f}")
                print(f"  {'TIME':>20s} | {'RATE/1H':>10s} | {'ANNUAL':>10s}")
                print("  " + "-" * 45)
                for e in entries[:8]:
                    rate = float(e.get("fundingRate") or 0)
                    ann = rate * 8760 * 100
                    t = ts(e.get("createdAt") or e.get("timestamp", 0))
                    print(f"  {t:>20s} | {rate*100:>9.4f}% | {ann:>+9.2f}%")
        except Exception as ex:
            print(f"  {ticker}: error fetching funding — {ex}")

    print(f"\n  Total unrealized funding: ${total_funding_unrealized:+,.4f}")


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {"funding": funding}
ACCT_SECTIONS = {
    "account": account,
    "positions": positions,
    "orders": orders,
    "fills": fills,
    "income": income,
}
ALL_SECTIONS = {**PUBLIC_SECTIONS, **ACCT_SECTIONS}
ALL_NAMES = list(ALL_SECTIONS.keys()) + ["all"]


async def main():
    args = sys.argv[1:]

    command = "all"
    address = DEFAULT_ADDRESS

    for arg in args:
        if arg.lower().startswith("0x") and len(arg) >= 40:
            address = arg
        elif arg.lower() in ALL_NAMES:
            command = arg.lower()

    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as client:
        if command == "all":
            await funding(client)
            print()
            if address:
                for name, fn in ACCT_SECTIONS.items():
                    try:
                        await fn(client, address)
                    except httpx.HTTPStatusError as e:
                        print(f"\n  {name}: HTTP {e.response.status_code} — {e.response.text[:100]}")
                    print()
            else:
                print("  [Account sections skipped — set ETHEREAL_WALLET_ADDRESS in .env]")
        elif command in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[command](client)
        elif command in ACCT_SECTIONS:
            if not address:
                print(f"ERROR: {command} requires ETHEREAL_WALLET_ADDRESS in .env or pass 0x... address")
                sys.exit(1)
            await ACCT_SECTIONS[command](client, address)
        else:
            print(f"Unknown section: {command}")
            print(f"Valid options: {', '.join(ALL_NAMES)}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
