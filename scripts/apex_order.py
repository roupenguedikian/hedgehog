#!/usr/bin/env python3
"""
Apex Omni limit order tool — used by the /apex-limit Claude skill.

Uses the apexomni Python SDK for L2-signed order placement.

Usage:
    python3 scripts/apex_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/apex_order.py BTC long 0.001 70000
    python3 scripts/apex_order.py ETH short 0.1 2100 --tif GTX
    python3 scripts/apex_order.py SOL long 10 90.5 --reduce-only
    python3 scripts/apex_order.py BTC long 0.001 70000 --dry-run
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sys
import time
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

BASE = "https://omni.apex.exchange"
API_KEY = os.environ.get("APEX_OMNI_API_KEY", "")
API_SECRET = os.environ.get("APEX_OMNI_API_SECRET", "")
PASSPHRASE = os.environ.get("APEX_OMNI_PASSPHRASE", "")
L2_KEY = os.environ.get("APEX_OMNI_L2_KEY", "")

HAS_AUTH = bool(API_KEY and API_SECRET and PASSPHRASE)


def sign_request(secret, timestamp, method, request_path, data_string=""):
    message = timestamp + method.upper() + request_path + data_string
    hmac_key = base64.standard_b64encode(secret.encode("utf-8"))
    sig = hmac.new(hmac_key, message.encode("utf-8"), hashlib.sha256)
    return base64.standard_b64encode(sig.digest()).decode()


def auth_headers(method, path, data_string=""):
    timestamp = str(int(round(time.time() * 1000)))
    sig = sign_request(API_SECRET, timestamp, method, path, data_string)
    return {
        "APEX-SIGNATURE": sig,
        "APEX-API-KEY": API_KEY,
        "APEX-TIMESTAMP": timestamp,
        "APEX-PASSPHRASE": PASSPHRASE,
    }


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/apex_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]")
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

    tif = "GOOD_TIL_CANCEL"
    dry_run = False
    reduce_only = False

    tif_map = {
        "GTC": "GOOD_TIL_CANCEL",
        "IOC": "IMMEDIATE_OR_CANCEL",
        "GTX": "POST_ONLY",
    }

    i = 4
    while i < len(args):
        if args[i] == "--tif" and i + 1 < len(args):
            tif_input = args[i + 1].upper()
            if tif_input not in tif_map:
                print(f"ERROR: Invalid TIF '{tif_input}'. Use: GTC, IOC, GTX")
                sys.exit(1)
            tif = tif_map[tif_input]
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

    if not HAS_AUTH:
        print("ERROR: APEX_OMNI_API_KEY, APEX_OMNI_API_SECRET, APEX_OMNI_PASSPHRASE must be set in .env")
        sys.exit(1)

    notional = size * price
    side_label = "BUY/LONG" if side == "BUY" else "SELL/SHORT"
    tif_short = {"GOOD_TIL_CANCEL": "GTC", "IMMEDIATE_OR_CANCEL": "IOC", "POST_ONLY": "GTX"}.get(tif, tif)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # ── Market context ──
        funding_rate = 0.0
        last_price = 0.0
        index_price = 0.0
        predicted_rate = 0.0

        try:
            resp = await client.get(f"{BASE}/api/v3/ticker", params={"symbol": symbol})
            items = resp.json().get("data", [])
            if items and isinstance(items, list) and items[0]:
                d = items[0]
                funding_rate = float(d.get("fundingRate", 0))
                predicted_rate = float(d.get("predictedFundingRate", 0))
                last_price = float(d.get("lastPrice", 0))
                index_price = float(d.get("indexPrice", 0))
        except Exception as e:
            print(f"  Warning: failed to fetch ticker: {e}")

        # Orderbook
        best_bid = 0.0
        best_ask = 0.0
        try:
            resp = await client.get(f"{BASE}/api/v3/depth", params={"symbol": symbol, "limit": 5})
            book = resp.json().get("data", {})
            bids = book.get("b", book.get("bids", []))
            asks = book.get("a", book.get("asks", []))
            if bids:
                best_bid = float(bids[0][0]) if isinstance(bids[0], list) else float(bids[0].get("price", 0))
            if asks:
                best_ask = float(asks[0][0]) if isinstance(asks[0], list) else float(asks[0].get("price", 0))
        except Exception as e:
            print(f"  Warning: failed to fetch orderbook: {e}")

        ann_rate = funding_rate * 1095  # 8h cycle
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else last_price
        spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 and best_bid and best_ask else 0
        dist_pct = (price - mid) / mid * 100 if mid > 0 else 0

        print("=" * 60)
        print(f"  APEX OMNI LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:       {symbol}")
        print(f"  Side:         {side_label}")
        print(f"  Size:         {size}")
        print(f"  Price:        ${price:,.4f}")
        print(f"  Notional:     ${notional:,.2f}")
        print(f"  TIF:          {tif_short}")
        print(f"  Reduce Only:  {reduce_only}")

        print(f"\n  --- Market Context ---")
        print(f"  Last Price:   ${last_price:,.4f}")
        print(f"  Index Price:  ${index_price:,.4f}")
        print(f"  Funding:      {funding_rate*100:.6f}%/8h ({ann_rate*100:.2f}% ann)")
        print(f"  Predicted:    {predicted_rate*100:.6f}%/8h")
        if best_bid and best_ask:
            print(f"  Best Bid:     ${best_bid:,.4f}")
            print(f"  Best Ask:     ${best_ask:,.4f}")
            print(f"  Spread:       {spread_bps:.2f} bps")
            print(f"  Limit vs Mid: {dist_pct:+.3f}%")

        # ── Account balance ──
        try:
            hdrs = auth_headers("GET", "/api/v3/account-balance")
            resp = await client.get(f"{BASE}/api/v3/account-balance", headers=hdrs)
            bal = resp.json().get("data", {})
            if isinstance(bal, dict):
                equity = float(bal.get("totalEquityValue", 0))
                available = float(bal.get("availableBalance", 0))
                init_margin = float(bal.get("initialMargin", 0))
                print(f"\n  --- Account ---")
                print(f"  NAV:          ${equity:,.2f}")
                print(f"  Free Margin:  ${available:,.2f}")
        except Exception as e:
            print(f"\n  --- Account ---")
            print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # ── Place order via SDK ──
        print(f"\n  Placing order...")

        try:
            from apexomni import HTTP as ApexHTTP
        except ImportError:
            print("  ERROR: apexomni SDK not installed. Install with: pip install apexomni")
            print("  The Apex order API requires L2 key signing via the SDK.")
            sys.exit(1)

        if not L2_KEY:
            print("  ERROR: APEX_OMNI_L2_KEY must be set in .env for order placement.")
            print("  Generate L2 keys via the Apex web interface or onboarding endpoint.")
            sys.exit(1)

        try:
            apex_client = ApexHTTP(
                endpoint=BASE,
                api_key_credentials={
                    "key": API_KEY,
                    "secret": API_SECRET,
                    "passphrase": PASSPHRASE,
                },
            )
            apex_client.stark_key_pair = {"public_key": "", "private_key": L2_KEY}

            # Expiration: 30 days from now
            expiration_epoch_seconds = int(time.time()) + 30 * 24 * 3600

            result = apex_client.create_order(
                symbol=symbol,
                side=side,
                type="LIMIT",
                size=str(size),
                price=str(price),
                limitFee="0.001",
                timeInForce=tif,
                reduceOnly=reduce_only,
                expirationEpochSeconds=expiration_epoch_seconds,
            )

            order_data = result.get("data", result)
            print(f"\n  --- Result ---")
            print(f"  Status:       {order_data.get('status', 'SUBMITTED')}")
            print(f"  Order ID:     {order_data.get('id', order_data.get('orderId', 'N/A'))}")
            if order_data.get("size"):
                print(f"  Size:         {order_data['size']}")
            if order_data.get("price"):
                print(f"  Price:        ${float(order_data['price']):,.4f}")

        except Exception as e:
            print(f"\n  --- Result ---")
            print(f"  Error:        {e}")
            return

        # ── Updated account ──
        try:
            hdrs2 = auth_headers("GET", "/api/v3/account-balance")
            resp2 = await client.get(f"{BASE}/api/v3/account-balance", headers=hdrs2)
            bal2 = resp2.json().get("data", {})
            if isinstance(bal2, dict):
                new_equity = float(bal2.get("totalEquityValue", 0))
                new_avail = float(bal2.get("availableBalance", 0))
                print(f"\n  --- Updated Account ---")
                print(f"  NAV:          ${new_equity:,.2f}")
                print(f"  Free Margin:  ${new_avail:,.2f}")
        except Exception:
            pass

        # ── Open orders ──
        try:
            hdrs3 = auth_headers("GET", "/api/v3/open-orders")
            resp3 = await client.get(f"{BASE}/api/v3/open-orders", headers=hdrs3)
            j3 = resp3.json()
            data3 = j3.get("data", {})
            order_list = data3.get("orders", data3) if isinstance(data3, dict) else data3
            if isinstance(order_list, list):
                sym_orders = [o for o in order_list if o.get("symbol") == symbol]
                if sym_orders:
                    print(f"\n  --- Open {symbol} Orders ---")
                    for o in sym_orders:
                        print(f"  {o.get('side','?')} {o.get('size','?')} @ "
                              f"${float(o.get('price',0)):,.4f}  id={o.get('id','?')}")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
