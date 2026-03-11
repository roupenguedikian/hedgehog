#!/usr/bin/env python3
"""
Aster DEX limit order tool — used by the /aster-limit Claude skill.

Usage:
    python3 scripts/aster_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/aster_order.py BTC long 0.001 70000
    python3 scripts/aster_order.py ETH short 0.1 2100 --tif GTX
    python3 scripts/aster_order.py SOL long 10 90.5 --reduce-only
    python3 scripts/aster_order.py BTC long 0.001 70000 --dry-run
"""
import asyncio
import hashlib
import hmac
import os
import sys
import time
from urllib.parse import urlencode

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

BASE = "https://fapi.asterdex.com"
API_KEY = os.environ.get("ASTER_API_KEY", "")
API_SECRET = os.environ.get("ASTER_API_SECRET", "")


def sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/aster_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (auto-appended USDT)")
        print("  side:         long or short (buy or sell)")
        print("  size:         quantity in base asset (e.g. 0.001 for BTC)")
        print("  price:        limit price in USD")
        print("  --tif:        time-in-force: GTC (default), IOC, GTX (post-only)")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show order details + market data but don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    side_str = args[1].lower()
    size = float(args[2])
    price = float(args[3])

    if side_str in ("long", "buy", "b"):
        side = "BUY"
    elif side_str in ("short", "sell", "s"):
        side = "SELL"
    else:
        print(f"ERROR: Invalid side '{side_str}'. Use: long/buy/b or short/sell/s")
        sys.exit(1)

    tif = "GTC"
    reduce_only = False
    dry_run = False

    i = 4
    while i < len(args):
        if args[i] == "--tif" and i + 1 < len(args):
            tif = args[i + 1].upper()
            if tif not in ("GTC", "IOC", "GTX"):
                print(f"ERROR: Invalid TIF '{tif}'. Use: GTC, IOC, GTX")
                sys.exit(1)
            i += 2
        elif args[i] == "--reduce-only":
            reduce_only = True
            i += 1
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"ERROR: Unknown flag '{args[i]}'")
            sys.exit(1)

    return symbol, side, size, price, tif, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, side, size, price, tif, reduce_only, dry_run = parse_args(args)

    if not API_KEY or not API_SECRET:
        print("ERROR: ASTER_API_KEY and ASTER_API_SECRET must be set in .env")
        sys.exit(1)

    headers = {
        "Content-Type": "application/json",
        "X-MBX-APIKEY": API_KEY,
    }

    notional = size * price
    side_label = "BUY/LONG" if side == "BUY" else "SELL/SHORT"

    async with httpx.AsyncClient(base_url=BASE, timeout=15.0, headers=headers) as client:
        # ── Market context ──
        funding_rate = 0.0
        mark_price = 0.0
        index_price = 0.0
        best_bid = 0.0
        best_ask = 0.0

        try:
            resp = await client.get("/fapi/v1/premiumIndex", params={"symbol": symbol})
            resp.raise_for_status()
            pi = resp.json()
            if isinstance(pi, list) and pi:
                pi = pi[0]
            funding_rate = float(pi.get("lastFundingRate", 0))
            mark_price = float(pi.get("markPrice", 0))
            index_price = float(pi.get("indexPrice", 0))
        except Exception as e:
            print(f"  Warning: failed to fetch premiumIndex: {e}")

        try:
            resp = await client.get("/fapi/v1/depth", params={"symbol": symbol, "limit": 5})
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids:
                best_bid = float(bids[0][0])
            if asks:
                best_ask = float(asks[0][0])
        except Exception as e:
            print(f"  Warning: failed to fetch orderbook: {e}")

        ann_rate = funding_rate * 1095  # 8h cycle
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else mark_price
        spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 and best_bid and best_ask else 0
        dist_pct = (price - mid) / mid * 100 if mid > 0 else 0

        print("=" * 60)
        print(f"  ASTER LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:       {symbol}")
        print(f"  Side:         {side_label}")
        print(f"  Size:         {size}")
        print(f"  Price:        ${price:,.4f}")
        print(f"  Notional:     ${notional:,.2f}")
        print(f"  TIF:          {tif}")
        print(f"  Reduce Only:  {reduce_only}")

        print(f"\n  --- Market Context ---")
        print(f"  Mark Price:   ${mark_price:,.4f}")
        print(f"  Index Price:  ${index_price:,.4f}")
        print(f"  Funding:      {funding_rate*100:.6f}%/8h ({ann_rate*100:.2f}% ann)")
        if best_bid and best_ask:
            print(f"  Best Bid:     ${best_bid:,.4f}")
            print(f"  Best Ask:     ${best_ask:,.4f}")
            print(f"  Spread:       {spread_bps:.2f} bps")
            print(f"  Limit vs Mid: {dist_pct:+.3f}%")

        # ── Account balance ──
        try:
            resp = await client.get("/fapi/v4/account", params=sign({}))
            resp.raise_for_status()
            acct = resp.json()
            wallet = float(acct.get("totalWalletBalance", 0))
            unrealized = float(acct.get("totalUnrealizedProfit", 0))
            margin_bal = float(acct.get("totalMarginBalance", 0))
            available = float(acct.get("availableBalance", 0))
            pos_margin = float(acct.get("totalPositionInitialMargin", 0))
            order_margin = float(acct.get("totalOpenOrderInitialMargin", 0))
            margin_used = pos_margin + order_margin
            print(f"\n  --- Account ---")
            print(f"  NAV:          ${margin_bal:,.2f}")
            print(f"  Free Margin:  ${available:,.2f}")
        except Exception as e:
            print(f"\n  --- Account ---")
            print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # ── Place order ──
        print(f"\n  Placing order...")
        order_params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": tif,
            "quantity": str(size),
            "price": str(price),
        }
        if reduce_only:
            order_params["reduceOnly"] = "true"

        signed_params = sign(order_params)
        resp = await client.post("/fapi/v1/order", params=signed_params)

        print(f"\n  --- Result ---")
        if resp.status_code == 200:
            result = resp.json()
            status = result.get("status", "UNKNOWN")
            order_id = result.get("orderId", "N/A")
            filled_qty = float(result.get("executedQty", 0))
            avg_px = float(result.get("avgPrice", 0))
            print(f"  Status:       {status}")
            print(f"  Order ID:     {order_id}")
            if filled_qty > 0:
                print(f"  Filled:       {filled_qty} @ ${avg_px:,.4f}")
        else:
            error = resp.json()
            print(f"  HTTP {resp.status_code}")
            print(f"  Error:        {error.get('msg', error)}")
            return

        # ── Updated account ──
        try:
            resp2 = await client.get("/fapi/v4/account", params=sign({}))
            resp2.raise_for_status()
            acct2 = resp2.json()
            new_margin = float(acct2.get("totalMarginBalance", 0))
            new_avail = float(acct2.get("availableBalance", 0))
            print(f"\n  --- Updated Account ---")
            print(f"  NAV:          ${new_margin:,.2f}")
            print(f"  Free Margin:  ${new_avail:,.2f}")
        except Exception:
            pass

        # ── Open orders ──
        try:
            resp3 = await client.get("/fapi/v1/openOrders", params=sign({"symbol": symbol}))
            resp3.raise_for_status()
            ords = resp3.json()
            if ords:
                print(f"\n  --- Open {symbol} Orders ---")
                for o in ords:
                    s = o.get("side", "?")
                    print(f"  {s} {o.get('origQty','?')} @ ${float(o.get('price',0)):,.4f}  "
                          f"id={o.get('orderId','?')}  status={o.get('status','?')}")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
