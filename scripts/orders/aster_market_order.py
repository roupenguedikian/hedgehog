#!/usr/bin/env python3
"""
Aster DEX market (taker) order tool — used by the /aster-market Claude skill.

Places a MARKET order for immediate execution at current market price.

Usage:
    python3 scripts/aster_market_order.py <symbol> <side> <size> [--reduce-only] [--dry-run]

Examples:
    python3 scripts/aster_market_order.py BTC long 0.001
    python3 scripts/aster_market_order.py ETH short 0.5 --reduce-only
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
    if len(args) < 3:
        print("Usage: python3 scripts/aster_market_order.py <symbol> <side> <size> [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (auto-appended USDT)")
        print("  side:         long/buy or short/sell")
        print("  size:         quantity in base asset")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show what would happen, don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    side_str = args[1].lower()
    size = float(args[2])

    if side_str in ("long", "buy", "b"):
        side = "BUY"
    elif side_str in ("short", "sell", "s"):
        side = "SELL"
    else:
        print(f"ERROR: Invalid side '{side_str}'. Use: long/buy or short/sell")
        sys.exit(1)

    reduce_only = False
    dry_run = False

    i = 3
    while i < len(args):
        if args[i] == "--reduce-only":
            reduce_only = True
            i += 1
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"ERROR: Unknown flag '{args[i]}'")
            sys.exit(1)

    return symbol, side, size, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, side, size, reduce_only, dry_run = parse_args(args)

    if not API_KEY or not API_SECRET:
        print("ERROR: ASTER_API_KEY and ASTER_API_SECRET must be set in .env")
        sys.exit(1)

    headers = {"Content-Type": "application/json", "X-MBX-APIKEY": API_KEY}
    side_label = "BUY/LONG" if side == "BUY" else "SELL/SHORT"

    async with httpx.AsyncClient(base_url=BASE, timeout=15.0, headers=headers) as client:
        # Market context
        mark_price = 0.0
        best_bid = 0.0
        best_ask = 0.0
        funding_rate = 0.0

        try:
            resp = await client.get("/fapi/v1/premiumIndex", params={"symbol": symbol})
            resp.raise_for_status()
            pi = resp.json()
            if isinstance(pi, list) and pi:
                pi = pi[0]
            mark_price = float(pi.get("markPrice", 0))
            funding_rate = float(pi.get("lastFundingRate", 0))
        except Exception:
            pass

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
        except Exception:
            pass

        ref_price = best_ask if side == "BUY" else best_bid
        if ref_price == 0:
            ref_price = mark_price
        est_notional = size * ref_price
        spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 10000 if best_bid and best_ask else 0

        print("=" * 60)
        print(f"  ASTER MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:        {symbol}")
        print(f"  Side:          {side_label}")
        print(f"  Size:          {size}")
        print(f"  Est. Price:    ${ref_price:,.4f}")
        print(f"  Est. Notional: ${est_notional:,.2f}")
        print(f"  Reduce Only:   {reduce_only}")

        print(f"\n  --- Market Context ---")
        print(f"  Mark Price:    ${mark_price:,.4f}")
        print(f"  Best Bid:      ${best_bid:,.4f}")
        print(f"  Best Ask:      ${best_ask:,.4f}")
        print(f"  Spread:        {spread_bps:.2f} bps")
        print(f"  Funding:       {funding_rate*100:.6f}%/8h ({funding_rate*1095*100:.2f}% ann)")

        # Account
        try:
            resp = await client.get("/fapi/v4/account", params=sign({}))
            resp.raise_for_status()
            acct = resp.json()
            margin_bal = float(acct.get("totalMarginBalance", 0))
            available = float(acct.get("availableBalance", 0))
            print(f"\n  --- Account ---")
            print(f"  NAV:           ${margin_bal:,.2f}")
            print(f"  Free Margin:   ${available:,.2f}")
        except Exception as e:
            print(f"\n  --- Account ---")
            print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # Place market order
        print(f"\n  Placing market order...")
        order_params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": str(size),
        }
        if reduce_only:
            order_params["reduceOnly"] = "true"

        resp = await client.post("/fapi/v1/order", params=sign(order_params))

        print(f"\n  --- Result ---")
        if resp.status_code == 200:
            result = resp.json()
            status = result.get("status", "UNKNOWN")
            order_id = result.get("orderId", "N/A")
            filled_qty = float(result.get("executedQty", 0))
            avg_px = float(result.get("avgPrice", 0))
            print(f"  Status:        {status}")
            print(f"  Order ID:      {order_id}")
            if filled_qty > 0:
                print(f"  Filled:        {filled_qty} @ ${avg_px:,.4f}")
                actual_bps = abs(avg_px - ref_price) / ref_price * 10000 if ref_price > 0 else 0
                print(f"  Slippage:      {actual_bps:.2f} bps")
        else:
            error = resp.json()
            print(f"  HTTP {resp.status_code}")
            print(f"  Error:         {error.get('msg', error)}")
            return

        # Updated account
        try:
            resp2 = await client.get("/fapi/v4/account", params=sign({}))
            resp2.raise_for_status()
            acct2 = resp2.json()
            new_margin = float(acct2.get("totalMarginBalance", 0))
            new_avail = float(acct2.get("availableBalance", 0))
            print(f"\n  --- Updated Account ---")
            print(f"  NAV:           ${new_margin:,.2f}")
            print(f"  Free Margin:   ${new_avail:,.2f}")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
