#!/usr/bin/env python3
"""
Apex Omni market (taker) order tool — used by the /apex-market Claude skill.

Uses the apexomni SDK for L2-signed market order placement.

Usage:
    python3 scripts/apex_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/apex_market_order.py BTC long 0.001
    python3 scripts/apex_market_order.py ETH short 0.5 --reduce-only
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sys
import time

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


def auth_headers(method, path):
    timestamp = str(int(round(time.time() * 1000)))
    sig = sign_request(API_SECRET, timestamp, method, path)
    return {
        "APEX-SIGNATURE": sig,
        "APEX-API-KEY": API_KEY,
        "APEX-TIMESTAMP": timestamp,
        "APEX-PASSPHRASE": PASSPHRASE,
    }


def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/apex_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:          BTC, ETH, SOL, etc. (auto-appended USDT)")
        print("  side:            long/buy or short/sell")
        print("  size:            quantity in base asset")
        print("  --slippage-bps N max slippage in bps (default: 50)")
        print("  --reduce-only    only reduce existing position")
        print("  --dry-run        show what would happen, don't execute")
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

    slippage_bps = 50
    reduce_only = False
    dry_run = False

    i = 3
    while i < len(args):
        if args[i] == "--slippage-bps" and i + 1 < len(args):
            slippage_bps = int(args[i + 1])
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

    return symbol, side, size, slippage_bps, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, side, size, slippage_bps, reduce_only, dry_run = parse_args(args)

    if not HAS_AUTH:
        print("ERROR: APEX_OMNI_API_KEY, APEX_OMNI_API_SECRET, APEX_OMNI_PASSPHRASE must be set in .env")
        sys.exit(1)

    side_label = "BUY/LONG" if side == "BUY" else "SELL/SHORT"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Market context
        last_price = 0.0
        funding_rate = 0.0

        try:
            resp = await client.get(f"{BASE}/api/v3/ticker", params={"symbol": symbol})
            items = resp.json().get("data", [])
            if items and isinstance(items, list) and items[0]:
                d = items[0]
                last_price = float(d.get("lastPrice", 0))
                funding_rate = float(d.get("fundingRate", 0))
        except Exception:
            pass

        # Slippage price for market order
        slippage_mult = slippage_bps / 10000
        if side == "BUY":
            worst_price = last_price * (1 + slippage_mult)
        else:
            worst_price = last_price * (1 - slippage_mult)

        est_notional = size * last_price

        print("=" * 60)
        print(f"  APEX OMNI MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:        {symbol}")
        print(f"  Side:          {side_label}")
        print(f"  Size:          {size}")
        print(f"  Slippage Cap:  {slippage_bps} bps")
        print(f"  Worst Price:   ${worst_price:,.4f}")
        print(f"  Est. Notional: ${est_notional:,.2f}")
        print(f"  Reduce Only:   {reduce_only}")

        print(f"\n  --- Market Context ---")
        print(f"  Last Price:    ${last_price:,.4f}")
        print(f"  Funding:       {funding_rate*100:.6f}%/8h ({funding_rate*1095*100:.2f}% ann)")

        # Account
        try:
            hdrs = auth_headers("GET", "/api/v3/account-balance")
            resp = await client.get(f"{BASE}/api/v3/account-balance", headers=hdrs)
            bal = resp.json().get("data", {})
            if isinstance(bal, dict):
                equity = float(bal.get("totalEquityValue", 0))
                available = float(bal.get("availableBalance", 0))
                print(f"\n  --- Account ---")
                print(f"  NAV:           ${equity:,.2f}")
                print(f"  Free Margin:   ${available:,.2f}")
        except Exception as e:
            print(f"\n  --- Account ---")
            print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # Place market order via SDK
        print(f"\n  Placing market order...")

        try:
            from apexomni import HTTP as ApexHTTP
        except ImportError:
            print("  ERROR: apexomni SDK not installed. Install with: pip install apexomni")
            sys.exit(1)

        if not L2_KEY:
            print("  ERROR: APEX_OMNI_L2_KEY must be set in .env for order placement.")
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

            expiration = int(time.time()) + 30 * 24 * 3600

            result = apex_client.create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                size=str(size),
                price=str(worst_price),
                limitFee="0.001",
                timeInForce="IMMEDIATE_OR_CANCEL",
                reduceOnly=reduce_only,
                expirationEpochSeconds=expiration,
            )

            order_data = result.get("data", result)
            print(f"\n  --- Result ---")
            print(f"  Status:        {order_data.get('status', 'SUBMITTED')}")
            print(f"  Order ID:      {order_data.get('id', order_data.get('orderId', 'N/A'))}")
            if order_data.get("avgPrice"):
                avg = float(order_data["avgPrice"])
                print(f"  Avg Price:     ${avg:,.4f}")
                actual_bps = abs(avg - last_price) / last_price * 10000
                print(f"  Slippage:      {actual_bps:.2f} bps")

        except Exception as e:
            print(f"\n  --- Result ---")
            print(f"  Error:         {e}")
            return

        # Updated account
        try:
            hdrs2 = auth_headers("GET", "/api/v3/account-balance")
            resp2 = await client.get(f"{BASE}/api/v3/account-balance", headers=hdrs2)
            bal2 = resp2.json().get("data", {})
            if isinstance(bal2, dict):
                new_equity = float(bal2.get("totalEquityValue", 0))
                new_avail = float(bal2.get("availableBalance", 0))
                print(f"\n  --- Updated Account ---")
                print(f"  NAV:           ${new_equity:,.2f}")
                print(f"  Free Margin:   ${new_avail:,.2f}")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
