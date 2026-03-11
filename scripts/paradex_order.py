#!/usr/bin/env python3
"""
Paradex limit order tool — used by the /paradex-limit Claude skill.

Uses the paradex-py SDK for StarkNet L2 signing and order placement.

Usage:
    python3 scripts/paradex_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|POST_ONLY] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/paradex_order.py BTC long 0.001 70000
    python3 scripts/paradex_order.py ETH short 0.1 2100 --tif POST_ONLY
    python3 scripts/paradex_order.py SOL long 10 90.5 --reduce-only
    python3 scripts/paradex_order.py BTC long 0.001 70000 --dry-run
"""
import os
import sys
from decimal import Decimal

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

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
load_env(os.path.join(_ROOT, ".env"))

L2_ADDRESS = os.environ.get("PARADEX_L2_ADDRESS", "")
L2_PRIVATE_KEY = os.environ.get("PARADEX_L2_PRIVATE_KEY", "")


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/paradex_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|POST_ONLY] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (auto-appended -USD-PERP)")
        print("  side:         long or short (buy or sell)")
        print("  size:         quantity in base asset (e.g. 0.001 for BTC)")
        print("  price:        limit price in USD")
        print("  --tif:        time-in-force: GTC (default), IOC, POST_ONLY (maker-only)")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show order details + market data but don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    if not symbol.endswith("-USD-PERP"):
        symbol = symbol + "-USD-PERP"
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

    valid_tifs = ("GTC", "IOC", "POST_ONLY")

    i = 4
    while i < len(args):
        if args[i] == "--tif" and i + 1 < len(args):
            tif = args[i + 1].upper()
            if tif not in valid_tifs:
                print(f"ERROR: Invalid TIF '{tif}'. Use: GTC, IOC, POST_ONLY")
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


def main():
    args = sys.argv[1:]
    symbol, side, size, price, tif, reduce_only, dry_run = parse_args(args)

    if not L2_ADDRESS or not L2_PRIVATE_KEY:
        print("ERROR: PARADEX_L2_ADDRESS and PARADEX_L2_PRIVATE_KEY must be set in .env")
        sys.exit(1)

    try:
        from paradex_py import Paradex
    except ImportError:
        print("ERROR: paradex-py not installed. Run: pip install paradex-py")
        sys.exit(1)

    # ── Initialize SDK ──
    try:
        paradex = Paradex(
            env="prod",
            l2_private_key=L2_PRIVATE_KEY,
        )
    except Exception as e:
        print(f"ERROR: Failed to initialize Paradex SDK — {e}")
        sys.exit(1)

    api = paradex.api_client
    notional = size * price
    side_label = "BUY/LONG" if side == "BUY" else "SELL/SHORT"

    # ── Validate symbol ──
    try:
        markets = api.fetch_markets()
        market_list = markets.get("results", [])
        valid_symbols = {m["symbol"] for m in market_list if m.get("asset_kind") == "PERP"}
        if symbol not in valid_symbols:
            print(f"ERROR: Symbol '{symbol}' not found on Paradex")
            print(f"Available: {', '.join(sorted(valid_symbols)[:30])}...")
            sys.exit(1)
        # Get funding period for this market
        market_info = next((m for m in market_list if m["symbol"] == symbol), {})
        cycle_h = int(market_info.get("funding_period_hours", 8))
    except Exception as e:
        print(f"ERROR: Failed to fetch markets — {e}")
        sys.exit(1)

    # ── Market context ──
    funding_rate = 0.0
    mark_price = 0.0
    underlying_price = 0.0
    best_bid = 0.0
    best_ask = 0.0

    try:
        summary = api.fetch_markets_summary({"market": symbol})
        results = summary.get("results", [])
        if results:
            s = results[0]
            funding_rate = float(s.get("funding_rate") or 0)
            mark_price = float(s.get("mark_price") or 0)
            underlying_price = float(s.get("underlying_price") or 0)
    except Exception as e:
        print(f"  Warning: failed to fetch market summary: {e}")

    try:
        bbo = api.fetch_bbo(symbol)
        best_bid = float(bbo.get("bid") or bbo.get("best_bid") or 0)
        best_ask = float(bbo.get("ask") or bbo.get("best_ask") or 0)
    except Exception:
        try:
            book = api.fetch_orderbook(symbol, params={"depth": 5})
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids:
                best_bid = float(bids[0][0]) if isinstance(bids[0], list) else float(bids[0].get("price", 0))
            if asks:
                best_ask = float(asks[0][0]) if isinstance(asks[0], list) else float(asks[0].get("price", 0))
        except Exception as e:
            print(f"  Warning: failed to fetch orderbook: {e}")

    payments_per_year = 365 * (24 / cycle_h)
    ann_rate = funding_rate * payments_per_year
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else mark_price
    spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 and best_bid and best_ask else 0
    dist_pct = (price - mid) / mid * 100 if mid > 0 else 0

    print("=" * 60)
    print(f"  PARADEX LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
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
    print(f"  Underlying:   ${underlying_price:,.4f}")
    print(f"  Funding:      {funding_rate*100:.6f}%/{cycle_h}h ({ann_rate*100:.2f}% ann)")
    if best_bid and best_ask:
        print(f"  Best Bid:     ${best_bid:,.4f}")
        print(f"  Best Ask:     ${best_ask:,.4f}")
        print(f"  Spread:       {spread_bps:.2f} bps")
        print(f"  Limit vs Mid: {dist_pct:+.3f}%")

    # ── Account balance ──
    try:
        acct = api.fetch_account_summary()
        account_value = float(acct.get("account_value") or 0)
        free_collateral = float(acct.get("free_collateral") or 0)
        total_collateral = float(acct.get("total_collateral") or 0)
        margin_used = total_collateral - free_collateral
        margin_util = (margin_used / total_collateral * 100) if total_collateral > 0 else 0
        print(f"\n  --- Account ---")
        print(f"  NAV:          ${account_value:,.2f}")
        print(f"  Free Margin:  ${free_collateral:,.2f}")
        print(f"  Margin Used:  ${margin_used:,.2f} ({margin_util:.1f}%)")
    except Exception as e:
        print(f"\n  --- Account ---")
        print(f"  Error: {e}")

    if dry_run:
        print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
        return

    # ── Place order via SDK ──
    print(f"\n  Placing order...")

    try:
        from paradex_py.api.generated import OrderType, OrderSide
        from paradex_py.api.api_client import Order

        order = Order(
            market=symbol,
            order_type=OrderType.LIMIT,
            order_side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            size=Decimal(str(size)),
            limit_price=Decimal(str(price)),
            instruction=tif,
            reduce_only=reduce_only,
        )

        result = api.submit_order(order)

        print(f"\n  --- Result ---")
        print(f"  Status:       {result.get('status', 'SUBMITTED')}")
        order_id = result.get("id") or result.get("order_id") or "N/A"
        print(f"  Order ID:     {order_id}")
        if result.get("size"):
            print(f"  Size:         {result['size']}")
        if result.get("price") or result.get("limit_price"):
            p = result.get("price") or result.get("limit_price")
            print(f"  Price:        ${float(p):,.4f}")
        if result.get("client_id"):
            print(f"  Client ID:    {result['client_id']}")

    except Exception as e:
        print(f"\n  --- Result ---")
        print(f"  Error:        {e}")
        return

    # ── Updated account ──
    try:
        acct2 = api.fetch_account_summary()
        new_value = float(acct2.get("account_value") or 0)
        new_free = float(acct2.get("free_collateral") or 0)
        print(f"\n  --- Updated Account ---")
        print(f"  NAV:          ${new_value:,.2f}")
        print(f"  Free Margin:  ${new_free:,.2f}")
    except Exception:
        pass

    # ── Open orders for this symbol ──
    try:
        orders_resp = api.fetch_orders({"market": symbol})
        order_list = orders_resp.get("results", [])
        if order_list:
            print(f"\n  --- Open {symbol} Orders ---")
            for o in order_list:
                remaining = float(o.get("remaining_size") or 0)
                orig = float(o.get("size") or 0)
                filled = orig - remaining
                print(f"  {o.get('side','?')} {orig} @ ${float(o.get('price',0)):,.4f}  "
                      f"filled={filled}  id={o.get('id','?')}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
