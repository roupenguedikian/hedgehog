#!/usr/bin/env python3
"""
Lighter ZK-rollup market (taker) order tool — used by the /lighter-market Claude skill.

Places an IOC limit order at an aggressive price to simulate a market order.

Usage:
    python3 scripts/lighter_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/lighter_market_order.py BTC long 0.001
    python3 scripts/lighter_market_order.py ETH short 0.5 --reduce-only
"""
import asyncio
import os
import sys

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

MARKET_IDS = {
    "ETH": 0, "BTC": 1, "SOL": 2, "DOGE": 3, "XRP": 7, "LINK": 8, "AVAX": 9,
    "NEAR": 10, "DOT": 11, "TON": 12, "POL": 14, "SUI": 16, "HYPE": 24, "BNB": 25,
    "AAVE": 27, "UNI": 30, "LTC": 35, "ADA": 39, "TRX": 43, "PUMP": 45, "BCH": 58,
    "XMR": 77, "SKY": 79, "ASTER": 83, "ZEC": 90, "XLM": 119,
}


def _get_signer(account_index):
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


def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/lighter_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:          BTC, ETH, SOL, etc.")
        print("  side:            long/buy or short/sell")
        print("  size:            quantity in base asset")
        print("  --slippage-bps N max slippage in bps (default: 10)")
        print("  --reduce-only    only reduce existing position")
        print("  --dry-run        show what would happen, don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    side_str = args[1].lower()
    size = float(args[2])

    if side_str in ("long", "buy", "b"):
        is_buy = True
    elif side_str in ("short", "sell", "s"):
        is_buy = False
    else:
        print(f"ERROR: Invalid side '{side_str}'. Use: long/buy or short/sell")
        sys.exit(1)

    slippage_bps = 10
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

    return symbol, is_buy, size, slippage_bps, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, is_buy, size, slippage_bps, reduce_only, dry_run = parse_args(args)

    if symbol not in MARKET_IDS:
        print(f"ERROR: Symbol '{symbol}' not found. Available: {', '.join(sorted(MARKET_IDS.keys()))}")
        sys.exit(1)

    market_id = MARKET_IDS[symbol]
    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20.0) as client:
        # Market context
        last_price = 0.0
        funding_rate = 0.0
        best_bid = 0.0
        best_ask = 0.0

        try:
            resp = await client.get("/api/v1/orderBookDetails")
            resp.raise_for_status()
            for d in resp.json().get("order_book_details", []):
                if d["market_id"] == market_id:
                    last_price = float(d.get("last_trade_price", 0))
                    break
        except Exception:
            pass

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

        ref_price = best_ask if is_buy else best_bid
        if ref_price == 0:
            ref_price = last_price

        # Aggressive IOC price with slippage cap
        slippage_mult = slippage_bps / 10000
        if is_buy:
            aggressive_price = ref_price * (1 + slippage_mult)
        else:
            aggressive_price = ref_price * (1 - slippage_mult)

        if aggressive_price > 1000:
            aggressive_price = round(aggressive_price, 2)
        elif aggressive_price > 1:
            aggressive_price = round(aggressive_price, 4)
        else:
            aggressive_price = round(aggressive_price, 6)

        est_notional = size * ref_price
        spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 10000 if best_bid and best_ask else 0

        print("=" * 60)
        print(f"  LIGHTER MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:        {symbol} (market_id={market_id})")
        print(f"  Side:          {side_label}")
        print(f"  Size:          {size}")
        print(f"  Slippage Cap:  {slippage_bps} bps")
        print(f"  Limit Price:   ${aggressive_price:,.4f} (IOC)")
        print(f"  Est. Notional: ${est_notional:,.2f}")
        print(f"  Reduce Only:   {reduce_only}")

        print(f"\n  --- Market Context ---")
        print(f"  Last Price:    ${last_price:,.4f}")
        print(f"  Best Bid:      ${best_bid:,.4f}")
        print(f"  Best Ask:      ${best_ask:,.4f}")
        print(f"  Spread:        {spread_bps:.2f} bps")
        print(f"  Funding:       {funding_rate*100:.6f}%/hr ({funding_rate*8760*100:.2f}% ann)")

        # Account
        try:
            resp = await client.get("/api/v1/account", params={"by": "index", "value": str(ACCOUNT_INDEX)})
            resp.raise_for_status()
            acct = resp.json()["accounts"][0]
            total_asset = float(acct["total_asset_value"])
            available = float(acct["available_balance"])
            print(f"\n  --- Account ---")
            print(f"  NAV:           ${total_asset:,.2f}")
            print(f"  Free Margin:   ${available:,.2f}")
        except Exception as e:
            print(f"\n  --- Account ---")
            print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # Place IOC order via lighter-sdk
        print(f"\n  Placing IOC order...")

        signer = _get_signer(ACCOUNT_INDEX)
        if not signer:
            print("  ERROR: Could not create signer")
            sys.exit(1)

        try:
            result, err = signer.create_order(
                market_index=market_id,
                amount=str(size),
                price=str(aggressive_price),
                is_ask=not is_buy,
                order_type=1,  # IOC
                api_key_index=API_KEY_INDEX,
            )

            print(f"\n  --- Result ---")
            if err:
                print(f"  Error:         {err}")
            elif result:
                if isinstance(result, dict):
                    print(f"  Status:        {result.get('status', 'SUBMITTED')}")
                    print(f"  Order ID:      {result.get('order_id', result.get('id', 'N/A'))}")
                else:
                    print(f"  Response:      {result}")
            else:
                print(f"  Order submitted (no error)")

        except Exception as e:
            print(f"\n  --- Result ---")
            print(f"  Error:         {e}")
            return

        # Updated account
        try:
            resp2 = await client.get("/api/v1/account", params={"by": "index", "value": str(ACCOUNT_INDEX)})
            resp2.raise_for_status()
            acct2 = resp2.json()["accounts"][0]
            print(f"\n  --- Updated Account ---")
            print(f"  NAV:           ${float(acct2['total_asset_value']):,.2f}")
            print(f"  Free Margin:   ${float(acct2['available_balance']):,.2f}")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
