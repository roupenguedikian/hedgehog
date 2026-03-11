#!/usr/bin/env python3
"""
Query Extended DEX (Starknet) for funding rates, markets, account info, positions, orders, fills.
Public endpoints need no auth. Private endpoints need X-Api-Key header.

Usage:
    python3 connectors/extended_query.py [funding|markets|account|positions|orders|fills|income|all]

Environment variables (.env):
    EXTENDED_API_KEY  — API key for private endpoints
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.starknet.extended.exchange/api/v1"
API_KEY = os.getenv("EXTENDED_API_KEY", "")

# ANSI colors
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
D = "\033[2m"
X = "\033[0m"

HEADERS = {"User-Agent": "hedgehog/1.0"}


def _auth_headers() -> dict:
    if not API_KEY:
        raise RuntimeError("EXTENDED_API_KEY not set in .env")
    return {**HEADERS, "X-Api-Key": API_KEY}


def _clean_symbol(market: str) -> str:
    """BTC-USD -> BTC"""
    return market.replace("-USD", "").replace("-PERP", "")


async def _get_all_markets(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all markets with embedded stats from /info/markets."""
    resp = await client.get(f"{BASE}/info/markets", headers=HEADERS)
    resp.raise_for_status()
    body = resp.json()
    # Response: {"status": "ok", "data": [...]}
    data = body.get("data", body) if isinstance(body, dict) else body
    return [m for m in data if isinstance(m, dict) and m.get("active")]


def _parse_market(m: dict) -> dict:
    """Extract normalized fields from a market object."""
    name = m.get("name", "")
    s = m.get("marketStats", {})
    return {
        "market": name,
        "symbol": _clean_symbol(name),
        "rate": float(s.get("fundingRate", 0) or 0),
        "mark": float(s.get("markPrice", 0) or 0),
        "index": float(s.get("indexPrice", 0) or 0),
        "last": float(s.get("lastPrice", 0) or 0),
        "volume": float(s.get("dailyVolume", 0) or 0),
        "oi": float(s.get("openInterest", 0) or 0),
        "change_pct": float(s.get("dailyPriceChangePercentage", 0) or 0),
        "high": float(s.get("dailyHigh", 0) or 0),
        "low": float(s.get("dailyLow", 0) or 0),
    }


# ═══════════════════════════════════════════════════════════════
# PUBLIC — no auth needed
# ═══════════════════════════════════════════════════════════════

async def query_funding(client: httpx.AsyncClient):
    """Funding rates for all markets + extreme funding scanner."""
    markets = await _get_all_markets(client)

    print("=" * 82)
    print("  Extended — FUNDING RATES")
    print("=" * 82)

    assets = [_parse_market(m) for m in markets]

    # Annualize: fundingRate is the hourly rate → 8760 payments/year
    for a in assets:
        a["annualized"] = a["rate"] * 8760 * 100

    by_vol = sorted(assets, key=lambda a: a["volume"], reverse=True)

    print(f"\n  {'SYMBOL':<12} {'RATE/1H':>12} {'ANNUAL':>8} {'MARK':>12} "
          f"{'24H CHG':>8} {'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 86)
    for a in by_vol[:25]:
        ann = a["annualized"]
        color = G if ann > 0 else R if ann < 0 else ""
        reset = X if color else ""
        print(f"  {a['symbol']:<12} {a['rate']*100:>11.6f}% "
              f"{color}{ann:>+7.2f}%{reset} "
              f"${a['mark']:>11,.2f} {a['change_pct']*100:>+7.2f}% "
              f"${a['oi']:>13,.0f} ${a['volume']:>13,.0f}")

    # Extreme funding
    by_rate = sorted(assets, key=lambda a: a["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':<12} {'ANNUAL':>8} {'MARK':>12} {'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 66)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:<12} {G}{a['annualized']:>+7.2f}%{X} "
              f"${a['mark']:>11,.4f} ${a['oi']:>13,.0f} ${a['volume']:>13,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:<12} {R}{a['annualized']:>+7.2f}%{X} "
              f"${a['mark']:>11,.4f} ${a['oi']:>13,.0f} ${a['volume']:>13,.0f}")

    total_vol = sum(a["volume"] for a in assets)
    total_oi = sum(a["oi"] for a in assets)
    avg_rate = sum(a["annualized"] for a in by_vol[:20]) / min(20, len(by_vol)) if by_vol else 0
    print(f"\n  Funding cycle: 1h (8760 payments/year, 8h rate / 8)")
    print(f"  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Total open interest: ${total_oi:,.0f}")
    print(f"  Active markets: {len(assets)}")
    print(f"  Avg annualized rate (top 20): {avg_rate:+.2f}%")


async def query_markets(client: httpx.AsyncClient):
    """All active markets with price, volume, OI."""
    markets = await _get_all_markets(client)

    print("=" * 82)
    print("  Extended — ALL MARKETS")
    print("=" * 82)

    rows = [_parse_market(m) for m in markets]
    rows.sort(key=lambda a: a["volume"], reverse=True)

    print(f"\n  {'SYMBOL':<12} {'LAST':>12} {'24H HIGH':>12} {'24H LOW':>12} "
          f"{'CHG%':>8} {'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 90)
    for a in rows:
        if a["volume"] < 1000:
            continue
        chg_c = G if a["change_pct"] > 0 else R if a["change_pct"] < 0 else ""
        print(f"  {a['symbol']:<12} ${a['last']:>11,.2f} ${a['high']:>11,.2f} ${a['low']:>11,.2f} "
              f"{chg_c}{a['change_pct']*100:>+7.2f}%{X} ${a['oi']:>13,.0f} ${a['volume']:>13,.0f}")

    print(f"\n  Total markets: {len(rows)} active")


# ═══════════════════════════════════════════════════════════════
# PRIVATE — X-Api-Key auth
# ═══════════════════════════════════════════════════════════════

def _unwrap(body) -> any:
    """Unwrap Extended's {"status": ..., "data": ...} envelope."""
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


async def query_account(client: httpx.AsyncClient):
    """Account balance, equity, available margin."""
    print("=" * 82)
    print("  Extended ACCOUNT")
    print("=" * 82)

    try:
        headers = _auth_headers()

        # Account info
        resp = await client.get(f"{BASE}/user/account/info", headers=headers)
        resp.raise_for_status()
        info = _unwrap(resp.json())
        acct_id = info.get("id", info.get("accountId", "N/A")) if isinstance(info, dict) else "N/A"
        print(f"\n  Account ID:  {acct_id}")

        # Balance
        resp = await client.get(f"{BASE}/user/balance", headers=headers)
        resp.raise_for_status()
        bal = _unwrap(resp.json())
        if isinstance(bal, list):
            bal = bal[0] if bal else {}

        equity = float(bal.get("equity", 0) or 0)
        available = float(bal.get("availableForTrade", bal.get("available", 0)) or 0)
        upnl = float(bal.get("unrealisedPnl", bal.get("unrealizedPnl", 0)) or 0)
        margin = float(bal.get("initialMargin", 0) or 0)
        margin_ratio = float(bal.get("marginRatio", 0) or 0)
        balance = float(bal.get("balance", 0) or 0)
        leverage = float(bal.get("leverage", 0) or 0)
        exposure = float(bal.get("exposure", 0) or 0)

        margin_util = (margin / equity * 100) if equity > 0 else 0

        print(f"  Balance:     ${balance:,.2f}")
        print(f"  Equity:      ${equity:,.2f}")
        print(f"  Available:   ${available:,.2f}")
        print(f"  Margin Used: ${margin:,.2f} ({margin_util:.1f}%)")
        print(f"  Unrealized:  ${upnl:+,.2f}")
        if exposure:
            print(f"  Exposure:    ${exposure:,.2f}")
        if margin_ratio:
            print(f"  Margin Ratio: {margin_ratio:.4f}")
        if leverage:
            print(f"  Leverage:    {leverage:.1f}x")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_positions(client: httpx.AsyncClient):
    """Open positions with size, value, PnL."""
    print("=" * 82)
    print("  Extended POSITIONS")
    print("=" * 82)

    try:
        headers = _auth_headers()
        resp = await client.get(f"{BASE}/user/positions", headers=headers)
        resp.raise_for_status()
        positions = _unwrap(resp.json())
        if not isinstance(positions, list):
            positions = positions.get("positions", []) if isinstance(positions, dict) else []

        if not positions:
            print("\n  No open positions")
            return

        print(f"\n  {'SYMBOL':<12} {'SIDE':<6} {'SIZE':>12} {'VALUE':>14} "
              f"{'ENTRY':>12} {'MARK':>12} {'uPnL':>12}")
        print("  " + "-" * 86)

        total_upnl = 0.0
        for p in positions:
            market = p.get("market", "")
            sym = _clean_symbol(market)
            side = p.get("side", "").upper()
            size = float(p.get("size", 0) or 0)
            value = float(p.get("value", 0) or 0)
            entry = float(p.get("openPrice", p.get("entryPrice", 0)) or 0)
            mark = float(p.get("markPrice", 0) or 0)
            upnl = float(p.get("unrealisedPnl", p.get("unrealizedPnl", 0)) or 0)
            total_upnl += upnl

            if abs(size) < 1e-12:
                continue

            upnl_c = G if upnl > 0 else R if upnl < 0 else ""
            print(f"  {sym:<12} {side:<6} {abs(size):>12.4f} ${abs(value):>13,.2f} "
                  f"${entry:>11,.2f} ${mark:>11,.2f} {upnl_c}${upnl:>+11,.2f}{X}")

        print(f"\n  Total uPnL: ${total_upnl:+,.2f}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_orders(client: httpx.AsyncClient):
    """Active open orders."""
    print("=" * 82)
    print("  Extended OPEN ORDERS")
    print("=" * 82)

    try:
        headers = _auth_headers()
        resp = await client.get(f"{BASE}/user/orders", headers=headers)
        resp.raise_for_status()
        orders = _unwrap(resp.json())
        if not isinstance(orders, list):
            orders = orders.get("orders", []) if isinstance(orders, dict) else []

        if not orders:
            print("\n  No open orders")
            return

        print(f"\n  {'SYMBOL':<12} {'SIDE':<6} {'SIZE':>10} {'PRICE':>14} "
              f"{'FILLED':>10} {'TYPE':<10} {'STATUS':<12}")
        print("  " + "-" * 80)
        for o in orders:
            market = o.get("market", "")
            sym = _clean_symbol(market)
            side = o.get("side", "?")
            size = o.get("qty", o.get("size", "0"))
            price = o.get("price", "0")
            filled = o.get("filledQty", o.get("filledSize", "0"))
            otype = o.get("type", "?")
            status = o.get("status", "?")
            print(f"  {sym:<12} {side:<6} {size:>10} ${float(price):>13,.2f} "
                  f"{filled:>10} {otype:<10} {status:<12}")

        print(f"\n  Total open orders: {len(orders)}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_fills(client: httpx.AsyncClient):
    """Recent trade fills."""
    print("=" * 82)
    print("  Extended RECENT FILLS")
    print("=" * 82)

    try:
        headers = _auth_headers()
        resp = await client.get(
            f"{BASE}/user/trades",
            params={"limit": "30"},
            headers=headers,
        )
        resp.raise_for_status()
        fills = _unwrap(resp.json())
        if not isinstance(fills, list):
            fills = fills.get("trades", []) if isinstance(fills, dict) else []

        if not fills:
            print("\n  No recent fills")
            return

        print(f"\n  {'TIME':<20} {'SYMBOL':<10} {'SIDE':<6} {'SIZE':>10} "
              f"{'PRICE':>14} {'VALUE':>14} {'FEE':>10} {'ROLE':<6}")
        print("  " + "-" * 96)
        for f in fills:
            market = f.get("market", "")
            sym = _clean_symbol(market)
            side = f.get("side", "?")
            size = float(f.get("filledQty", f.get("size", 0)) or 0)
            price = float(f.get("averagePrice", f.get("price", 0)) or 0)
            value = float(f.get("value", size * price) or 0)
            fee = float(f.get("fee", 0) or 0)
            is_taker = f.get("isTaker", None)
            role = "TAKER" if is_taker else "MAKER" if is_taker is not None else "?"

            created = f.get("createdTime", f.get("timestamp", ""))
            if created and str(created).isdigit():
                ts = datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc)
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            elif created:
                ts_str = str(created)[:19]
            else:
                ts_str = "N/A"

            print(f"  {ts_str:<20} {sym:<10} {side:<6} {size:>10.4f} "
                  f"${price:>13,.2f} ${value:>13,.2f} "
                  f"${fee:>9,.4f} {role:<6}")

        print(f"\n  Total fills shown: {len(fills)}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_funding_payments(client: httpx.AsyncClient):
    """Funding payment history."""
    print("=" * 82)
    print("  Extended FUNDING PAYMENTS")
    print("=" * 82)

    try:
        headers = _auth_headers()
        resp = await client.get(
            f"{BASE}/user/funding/history",
            params={"limit": "100"},
            headers=headers,
        )
        resp.raise_for_status()
        payments = _unwrap(resp.json())
        if not isinstance(payments, list):
            payments = payments.get("payments", []) if isinstance(payments, dict) else []

        if not payments:
            print("\n  No funding payments found")
            return

        # Aggregate by market
        by_market: dict[str, list] = {}
        for p in payments:
            market = p.get("market", p.get("symbol", "?"))
            by_market.setdefault(market, []).append(p)

        print(f"\n  {'SYMBOL':<12} {'PAYMENTS':>8} {'TOTAL':>12} {'LATEST':>20}")
        print("  " + "-" * 56)

        grand_total = 0.0
        for market, pays in sorted(by_market.items()):
            sym = _clean_symbol(market)
            total = sum(
                float(p.get("amount", p.get("fundingPayment", p.get("payment", 0))) or 0)
                for p in pays
            )
            grand_total += total

            latest_time = ""
            for p in pays:
                t = p.get("createdTime", p.get("timestamp", ""))
                if str(t) > str(latest_time):
                    latest_time = t

            if latest_time and str(latest_time).isdigit():
                latest_dt = datetime.fromtimestamp(int(latest_time) / 1000, tz=timezone.utc)
                latest_str = latest_dt.strftime("%Y-%m-%d %H:%M")
            elif latest_time:
                latest_str = str(latest_time)[:16]
            else:
                latest_str = "N/A"

            color = G if total > 0 else R if total < 0 else ""
            print(f"  {sym:<12} {len(pays):>8} {color}${total:>+11,.4f}{X} {latest_str:>20}")

        print(f"\n  Grand total: ${grand_total:+,.4f} across {len(payments)} payments")

        # Last 10 individual payments
        print(f"\n  Last 10 payments:")
        print(f"  {'TIME':<20} {'SYMBOL':<10} {'AMOUNT':>12}")
        print("  " + "-" * 44)
        for p in payments[:10]:
            market = p.get("market", p.get("symbol", "?"))
            sym = _clean_symbol(market)
            amt = float(p.get("amount", p.get("fundingPayment", p.get("payment", 0))) or 0)
            ts = p.get("createdTime", p.get("timestamp", ""))
            if ts and str(ts).isdigit():
                dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            elif ts:
                ts_str = str(ts)[:16]
            else:
                ts_str = "N/A"
            color = G if amt > 0 else R if amt < 0 else ""
            print(f"  {ts_str:<20} {sym:<10} {color}${amt:>+11,.4f}{X}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {
    "funding": query_funding,
    "markets": query_markets,
}
PRIVATE_SECTIONS = {
    "account": query_account,
    "positions": query_positions,
    "orders": query_orders,
    "fills": query_fills,
    "income": query_funding_payments,
}
ALL_SECTIONS = {**PUBLIC_SECTIONS, **PRIVATE_SECTIONS}
ALL_NAMES = list(ALL_SECTIONS.keys()) + ["all"]


async def main():
    args = sys.argv[1:]
    command = args[0].lower() if args and args[0].lower() in ALL_NAMES else "all"

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    async with httpx.AsyncClient(timeout=30.0) as client:
        if command == "all":
            print(f"\n  Extended Full Query — {ts_str}\n")
            await query_funding(client)
            print()
            if API_KEY:
                await query_account(client)
                print()
                await query_positions(client)
                print()
                await query_orders(client)
                print()
                await query_fills(client)
                print()
                await query_funding_payments(client)
            else:
                print(f"  {Y}Skipping private endpoints — EXTENDED_API_KEY not set{X}")
        elif command in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[command](client)
        elif command in PRIVATE_SECTIONS:
            if not API_KEY:
                print("Error: EXTENDED_API_KEY not set in .env")
                sys.exit(1)
            await PRIVATE_SECTIONS[command](client)
        else:
            print(f"Unknown command: {command}")
            print(f"Valid: {', '.join(ALL_NAMES)}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
