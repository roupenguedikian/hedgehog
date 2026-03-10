#!/usr/bin/env python3
"""
Query dYdX v4 indexer for funding rates, prices, account info.
Uses the public indexer REST API (no auth needed for reads).

Usage:
    python3 scripts/dydx_query.py [funding|markets|account|positions|orders|fills|all] [address]
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx

load_dotenv()

BASE = "https://indexer.dydx.trade/v4"
DEFAULT_ADDRESS = os.getenv("DYDX_WALLET_ADDRESS", "")

TOP_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "SUI-USD",
               "AVAX-USD", "LINK-USD", "ARB-USD", "WIF-USD", "PEPE-USD",
               "TRX-USD", "ADA-USD", "XRP-USD", "ONDO-USD", "SEI-USD"]


async def query_markets(client: httpx.AsyncClient):
    """All perpetual markets with prices and current funding rates."""
    print("=" * 80)
    print("  dYdX v4 — PERPETUAL MARKETS (PRICES & FUNDING RATES)")
    print("=" * 80)

    resp = await client.get("/perpetualMarkets")
    resp.raise_for_status()
    markets = resp.json().get("markets", {})

    rows = []
    for ticker, m in markets.items():
        if m.get("status") != "ACTIVE":
            continue
        rate = float(m.get("nextFundingRate", 0))
        oracle = float(m.get("oraclePrice", 0))
        oi = float(m.get("openInterest", 0))
        volume24h = float(m.get("volume24H", 0))
        annualized = rate * 8760 * 100
        rows.append({
            "ticker": ticker,
            "oracle_price": oracle,
            "funding_rate": rate,
            "annualized_pct": annualized,
            "open_interest": oi,
            "volume_24h": volume24h,
        })

    # Top 20 by volume
    by_vol = sorted(rows, key=lambda r: r["volume_24h"], reverse=True)
    print(f"\n  {'TICKER':<14} {'RATE/HR':>10} {'ANNUAL':>8} {'ORACLE':>12} "
          f"{'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 80)
    for r in by_vol[:20]:
        oi_usd = r["open_interest"] * r["oracle_price"]
        print(f"  {r['ticker']:<14} {r['funding_rate']*100:>9.6f}% {r['annualized_pct']:>+7.2f}% "
              f"${r['oracle_price']:>11,.2f} ${oi_usd:>13,.0f} ${r['volume_24h']:>13,.0f}")

    # Extreme funding
    by_rate = sorted(rows, key=lambda r: r["annualized_pct"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'TICKER':<14} {'ANNUAL':>8} {'ORACLE':>12} {'OI (USD)':>14}")
    print("  " + "-" * 55)
    print("  -- HIGHEST --")
    for r in by_rate[:5]:
        oi_usd = r["open_interest"] * r["oracle_price"]
        print(f"  {r['ticker']:<14} {r['annualized_pct']:>+7.2f}% ${r['oracle_price']:>11,.4f} ${oi_usd:>13,.0f}")
    print("  -- MOST NEGATIVE --")
    for r in by_rate[-5:]:
        oi_usd = r["open_interest"] * r["oracle_price"]
        print(f"  {r['ticker']:<14} {r['annualized_pct']:>+7.2f}% ${r['oracle_price']:>11,.4f} ${oi_usd:>13,.0f}")

    total_vol = sum(r["volume_24h"] for r in rows)
    total_oi = sum(r["open_interest"] * r["oracle_price"] for r in rows)
    avg_rate = sum(r["annualized_pct"] for r in by_vol[:20]) / min(20, len(by_vol)) if by_vol else 0
    print(f"\n  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Total open interest: ${total_oi:,.0f}")
    print(f"  Active markets: {len(rows)}")
    print(f"  Avg annualized rate (top 20): {avg_rate:+.2f}%")


async def query_funding(client: httpx.AsyncClient):
    """Recent funding history for top symbols."""
    print("=" * 80)
    print("  dYdX v4 — FUNDING HISTORY (last 5 entries)")
    print("=" * 80)

    for symbol in TOP_SYMBOLS:
        try:
            resp = await client.get(f"/historicalFunding/{symbol}", params={"limit": 5})
            resp.raise_for_status()
            entries = resp.json().get("historicalFunding", [])
            if entries:
                print(f"\n  {symbol}:")
                for e in entries:
                    rate = float(e.get("rate", 0))
                    ts = e.get("effectiveAt", "")
                    ann = rate * 8760 * 100
                    color = "\033[92m" if ann > 0 else "\033[91m" if ann < 0 else ""
                    reset = "\033[0m" if color else ""
                    print(f"    {ts[:19]}  rate={rate:+.6f}  {color}annualized={ann:+.2f}%{reset}")
        except Exception as ex:
            print(f"  {symbol}: error — {ex}")


async def query_account(client: httpx.AsyncClient, address: str):
    """Account balances and equity."""
    print("=" * 80)
    print(f"  ACCOUNT: {address}")
    print("=" * 80)

    try:
        resp = await client.get(f"/addresses/{address}")
        resp.raise_for_status()
        acct = resp.json()
        subaccounts = acct.get("subaccounts", [])
        print(f"\n  Subaccounts: {len(subaccounts)}")
        for sa in subaccounts:
            sn = sa.get("subaccountNumber", "?")
            equity = float(sa.get("equity", 0))
            free_collateral = float(sa.get("freeCollateral", 0))
            margin_used = equity - free_collateral
            margin_enabled = sa.get("marginEnabled", False)
            print(f"\n  -- Subaccount #{sn} {'(margin)' if margin_enabled else ''}")
            print(f"     Equity (NAV):    ${equity:,.2f}")
            print(f"     Margin Used:     ${margin_used:,.2f}")
            print(f"     Free Margin:     ${free_collateral:,.2f}")

            asset_positions = sa.get("assetPositions", {})
            if asset_positions:
                print(f"     Asset Positions:")
                for asset_id, ap in asset_positions.items():
                    size = float(ap.get("size", 0))
                    side = ap.get("side", "")
                    symbol = ap.get("symbol", asset_id)
                    print(f"       {symbol}: {'+' if side == 'LONG' else '-'}{size:,.2f}")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  Account not found on dYdX indexer (no on-chain activity)")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_positions(client: httpx.AsyncClient, address: str):
    """Open perpetual positions."""
    print("=" * 80)
    print(f"  POSITIONS: {address}")
    print("=" * 80)

    try:
        resp = await client.get(f"/addresses/{address}")
        resp.raise_for_status()
        acct = resp.json()
        any_pos = False
        for sa in acct.get("subaccounts", []):
            sn = sa.get("subaccountNumber", "?")
            perp_positions = sa.get("openPerpetualPositions", {})
            if perp_positions:
                print(f"\n  Subaccount #{sn}:")
                for market, pp in perp_positions.items():
                    size = float(pp.get("size", 0))
                    side = pp.get("side", "")
                    entry = float(pp.get("entryPrice", 0))
                    unrealized = float(pp.get("unrealizedPnl", 0))
                    realized = float(pp.get("realizedPnl", 0))
                    sum_open = float(pp.get("sumOpen", 0))
                    sum_close = float(pp.get("sumClose", 0))
                    print(f"    {market}: {side} {abs(size):.4f} @ ${entry:,.2f}  "
                          f"uPnL: ${unrealized:+,.2f}  rPnL: ${realized:+,.2f}  "
                          f"open={sum_open:.4f} close={sum_close:.4f}")
                    any_pos = True
        if not any_pos:
            print("\n  No open perpetual positions")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  Account not found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_orders(client: httpx.AsyncClient, address: str):
    """Open orders."""
    print("=" * 80)
    print(f"  OPEN ORDERS: {address}")
    print("=" * 80)

    try:
        resp = await client.get("/orders", params={
            "address": address,
            "subaccountNumber": 0,
            "status": "OPEN",
            "limit": 50,
        })
        resp.raise_for_status()
        orders = resp.json()
        order_list = orders if isinstance(orders, list) else orders.get("orders", orders.get("results", []))
        if order_list:
            print(f"\n  {'Ticker':<14} {'Side':<6} {'Size':>10} {'Price':>14} {'Type':<10} {'Status':<10}")
            print("  " + "-" * 70)
            for o in order_list:
                print(f"  {o.get('ticker','?'):<14} {o.get('side',''):<6} {o.get('size',''):>10} "
                      f"${float(o.get('price', 0)):>13,.2f} {o.get('type',''):<10} {o.get('status',''):<10}")
        else:
            print("\n  No open orders")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  No orders found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_transfers(client: httpx.AsyncClient, address: str):
    """Recent transfers and funding payments."""
    print("=" * 80)
    print(f"  FUNDING PAYMENTS & TRANSFERS: {address}")
    print("=" * 80)

    try:
        resp = await client.get("/transfers", params={
            "address": address,
            "subaccountNumber": 0,
            "limit": 20,
        })
        resp.raise_for_status()
        data = resp.json()
        transfers = data if isinstance(data, list) else data.get("transfers", [])
        if transfers:
            print(f"\n  {'Time':<20} {'Type':<16} {'Size':>12} {'Sender':<14} {'Recipient':<14}")
            print("  " + "-" * 80)
            for t in transfers:
                print(f"  {t.get('createdAt','')[:19]:<20} {t.get('type',''):<16} "
                      f"${float(t.get('size', 0)):>11,.2f} "
                      f"{str(t.get('sender',{}).get('subaccountNumber','')):<14} "
                      f"{str(t.get('recipient',{}).get('subaccountNumber','')):<14}")
        else:
            print("\n  No recent transfers")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  No transfers found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_fills(client: httpx.AsyncClient, address: str):
    """Recent fills."""
    print("=" * 80)
    print(f"  RECENT FILLS: {address}")
    print("=" * 80)

    try:
        resp = await client.get("/fills", params={
            "address": address,
            "subaccountNumber": 0,
            "limit": 10,
        })
        resp.raise_for_status()
        fills_data = resp.json()
        fills = fills_data if isinstance(fills_data, list) else fills_data.get("fills", [])
        if fills:
            print(f"\n  {'Time':<20} {'Market':<12} {'Side':<6} {'Size':>10} {'Price':>14} {'Fee':>10}")
            print("  " + "-" * 78)
            for f in fills:
                print(f"  {f.get('createdAt','')[:19]:<20} {f.get('market','?'):<12} "
                      f"{f.get('side',''):<6} {f.get('size',''):>10} "
                      f"${float(f.get('price', 0)):>13,.2f} ${float(f.get('fee', 0)):>9,.4f}")
        else:
            print("\n  No recent fills")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  No fills found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


async def main():
    args = sys.argv[1:]

    # Parse command and optional address override
    command = "all"
    address = DEFAULT_ADDRESS

    for arg in args:
        if arg.startswith("dydx1"):
            address = arg
        elif arg in ("funding", "markets", "account", "positions", "orders", "fills", "transfers", "all"):
            command = arg

    if command in ("account", "positions", "orders", "fills", "transfers", "all") and not address:
        print("Error: DYDX_WALLET_ADDRESS not set in .env and no address provided")
        sys.exit(1)

    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as client:
        if command == "markets":
            await query_markets(client)
        elif command == "funding":
            await query_funding(client)
        elif command == "account":
            await query_account(client, address)
        elif command == "positions":
            await query_positions(client, address)
        elif command == "orders":
            await query_orders(client, address)
        elif command == "fills":
            await query_fills(client, address)
        elif command == "transfers":
            await query_transfers(client, address)
        elif command == "all":
            await query_markets(client)
            print()
            await query_funding(client)
            print()
            await query_account(client, address)
            print()
            await query_positions(client, address)
            print()
            await query_orders(client, address)
            print()
            await query_fills(client, address)
            print()
            await query_transfers(client, address)


if __name__ == "__main__":
    asyncio.run(main())
