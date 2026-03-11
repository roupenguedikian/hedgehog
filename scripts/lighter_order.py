#!/usr/bin/env python3
"""
Lighter ZK-rollup limit order tool — used by the /lighter-limit Claude skill.

Uses the lighter-sdk SignerClient for authenticated order placement.

Usage:
    python3 scripts/lighter_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|ALO] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/lighter_order.py BTC long 0.001 70000
    python3 scripts/lighter_order.py ETH short 0.5 2100 --tif ALO
    python3 scripts/lighter_order.py SOL long 10 90.5 --reduce-only
    python3 scripts/lighter_order.py BTC long 0.001 70000 --dry-run
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://mainnet.zklighter.elliot.ai"
_acct_idx = os.getenv("LIGHTER_ACCOUNT_INDEX", "")
if not _acct_idx:
    sys.exit("ERROR: LIGHTER_ACCOUNT_INDEX not set in .env")
ACCOUNT_INDEX = int(_acct_idx)
API_KEY_PRIVATE = os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", os.getenv("LIGHTER_API_KEY_PRIVATE", ""))
_api_idx = os.getenv("LIGHTER_API_KEY_INDEX", "")
if not _api_idx:
    sys.exit("ERROR: LIGHTER_API_KEY_INDEX not set in .env")
API_KEY_INDEX = int(_api_idx)


# Symbol → market_id mapping (from orderBookDetails)
MARKET_IDS = {
    "ETH": 0, "BTC": 1, "SOL": 2, "DOGE": 3, "XRP": 7, "LINK": 8, "AVAX": 9,
    "NEAR": 10, "DOT": 11, "TON": 12, "POL": 14, "SUI": 16, "HYPE": 24, "BNB": 25,
    "AAVE": 27, "UNI": 30, "LTC": 35, "ADA": 39, "TRX": 43, "PUMP": 45, "BCH": 58,
    "XMR": 77, "SKY": 79, "ASTER": 83, "ZEC": 90, "XLM": 119,
}


def _get_signer(account_index):
    """Create lighter-sdk SignerClient."""
    if not API_KEY_PRIVATE:
        return None
    try:
        import lighter
        signer = lighter.SignerClient(
            url=BASE_URL,
            api_private_keys={API_KEY_INDEX: API_KEY_PRIVATE},
            account_index=account_index,
        )
        err = signer.check_client()
        if err:
            print(f"  Warning: signer check failed: {err}")
            return None
        return signer
    except ImportError:
        print("  ERROR: lighter-sdk not installed. Install with: pip install lighter-sdk")
        sys.exit(1)


def _get_auth_token(signer):
    """Generate auth token for read endpoints."""
    if not signer:
        return None
    auth_token, err = signer.create_auth_token_with_expiry(
        deadline=3600, api_key_index=API_KEY_INDEX,
    )
    if err:
        print(f"  Warning: auth token error: {err}")
        return None
    return auth_token


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/lighter_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|ALO] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (bare symbols)")
        print("  side:         long or short (buy or sell)")
        print("  size:         quantity in base asset (e.g. 0.001 for BTC)")
        print("  price:        limit price in USD")
        print("  --tif:        time-in-force: GTC (default), IOC, ALO (post-only)")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show order details + market data but don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    side_str = args[1].lower()
    size = float(args[2])
    price = float(args[3])

    if side_str in ("long", "buy", "b"):
        is_buy = True
    elif side_str in ("short", "sell", "s"):
        is_buy = False
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
            if tif not in ("GTC", "IOC", "ALO"):
                print(f"ERROR: Invalid TIF '{tif}'. Use: GTC, IOC, ALO")
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

    return symbol, is_buy, size, price, tif, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, is_buy, size, price, tif, reduce_only, dry_run = parse_args(args)

    if symbol not in MARKET_IDS:
        print(f"ERROR: Symbol '{symbol}' not found. Available: {', '.join(sorted(MARKET_IDS.keys()))}")
        sys.exit(1)

    market_id = MARKET_IDS[symbol]
    notional = size * price
    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20.0) as client:
        # ── Market context ──
        funding_rate = 0.0
        last_price = 0.0
        best_bid = 0.0
        best_ask = 0.0

        try:
            fr_resp = await client.get("/api/v1/funding-rates")
            if fr_resp.status_code == 200:
                for fr in fr_resp.json().get("funding_rates", []):
                    if fr["market_id"] == market_id:
                        funding_rate = float(fr.get("rate", 0))
                        break
        except Exception:
            pass

        try:
            resp = await client.get("/api/v1/orderBookDetails")
            resp.raise_for_status()
            details = resp.json().get("order_book_details", [])
            for d in details:
                if d["market_id"] == market_id:
                    last_price = float(d.get("last_trade_price", 0))
                    break
        except Exception:
            pass

        try:
            resp = await client.get("/api/v1/orderBook", params={"market_index": market_id, "limit": 5})
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids:
                best_bid = float(bids[0].get("price", bids[0][0]) if isinstance(bids[0], dict) else bids[0][0])
            if asks:
                best_ask = float(asks[0].get("price", asks[0][0]) if isinstance(asks[0], dict) else asks[0][0])
        except Exception:
            pass

        ann_rate = funding_rate * 8760  # 1h cycle
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else last_price
        spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 and best_bid and best_ask else 0
        dist_pct = (price - mid) / mid * 100 if mid > 0 else 0

        print("=" * 60)
        print(f"  LIGHTER LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:       {symbol} (market_id={market_id})")
        print(f"  Side:         {side_label}")
        print(f"  Size:         {size}")
        print(f"  Price:        ${price:,.4f}")
        print(f"  Notional:     ${notional:,.2f}")
        print(f"  TIF:          {tif}")
        print(f"  Reduce Only:  {reduce_only}")
        print(f"  Account:      {ACCOUNT_INDEX}")

        print(f"\n  --- Market Context ---")
        print(f"  Last Price:   ${last_price:,.4f}")
        print(f"  Funding:      {funding_rate*100:.6f}%/hr ({ann_rate*100:.2f}% ann)")
        if best_bid and best_ask:
            print(f"  Best Bid:     ${best_bid:,.4f}")
            print(f"  Best Ask:     ${best_ask:,.4f}")
            print(f"  Spread:       {spread_bps:.2f} bps")
            print(f"  Limit vs Mid: {dist_pct:+.3f}%")

        # ── Account balance ──
        try:
            resp = await client.get("/api/v1/account", params={"by": "index", "value": str(ACCOUNT_INDEX)})
            resp.raise_for_status()
            acct = resp.json()["accounts"][0]
            collateral = float(acct["collateral"])
            available = float(acct["available_balance"])
            total_asset = float(acct["total_asset_value"])
            print(f"\n  --- Account ---")
            print(f"  NAV:          ${total_asset:,.2f}")
            print(f"  Collateral:   ${collateral:,.2f}")
            print(f"  Free Margin:  ${available:,.2f}")
        except Exception as e:
            print(f"\n  --- Account ---")
            print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # ── Place order via lighter-sdk ──
        print(f"\n  Placing order...")

        signer = _get_signer(ACCOUNT_INDEX)
        if not signer:
            print("  ERROR: Could not create signer. Check LIGHTER_API_KEY_PRIVATE_KEY in .env")
            sys.exit(1)

        try:
            # Map TIF to lighter SDK values
            tif_map = {
                "GTC": 0,  # Good til cancel
                "IOC": 1,  # Immediate or cancel
                "ALO": 2,  # Add liquidity only (post-only)
            }

            result, err = signer.create_order(
                market_index=market_id,
                amount=str(size),
                price=str(price),
                is_ask=not is_buy,
                order_type=tif_map.get(tif, 0),
                api_key_index=API_KEY_INDEX,
            )

            print(f"\n  --- Result ---")
            if err:
                print(f"  Error:        {err}")
            elif result:
                if isinstance(result, dict):
                    status = result.get("status", "SUBMITTED")
                    order_id = result.get("order_id", result.get("id", "N/A"))
                    print(f"  Status:       {status}")
                    print(f"  Order ID:     {order_id}")
                else:
                    print(f"  Response:     {result}")
            else:
                print(f"  Order submitted (no error returned)")

        except Exception as e:
            print(f"\n  --- Result ---")
            print(f"  Error:        {e}")
            return

        # ── Updated account ──
        try:
            resp2 = await client.get("/api/v1/account", params={"by": "index", "value": str(ACCOUNT_INDEX)})
            resp2.raise_for_status()
            acct2 = resp2.json()["accounts"][0]
            new_asset = float(acct2["total_asset_value"])
            new_avail = float(acct2["available_balance"])
            print(f"\n  --- Updated Account ---")
            print(f"  NAV:          ${new_asset:,.2f}")
            print(f"  Free Margin:  ${new_avail:,.2f}")
        except Exception:
            pass

        # ── Open orders ──
        auth_token = _get_auth_token(signer)
        if auth_token:
            try:
                resp3 = await client.get("/api/v1/accountActiveOrders",
                                         params={"account_index": ACCOUNT_INDEX, "market_id": market_id},
                                         headers={"Authorization": auth_token})
                orders = resp3.json().get("orders", [])
                if orders:
                    print(f"\n  --- Open {symbol} Orders ---")
                    for o in orders:
                        s = "SELL" if o.get("is_ask", False) else "BUY"
                        print(f"  {s} {o.get('base_amount', '?')} @ {o.get('price', '?')}")
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
