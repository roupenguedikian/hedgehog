#!/usr/bin/env python3
"""
Hyperliquid limit order tool — used by the /hl-order Claude skill.

Usage:
    python3 scripts/hl_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|ALO] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/hl_order.py BTC long 0.001 70000
    python3 scripts/hl_order.py ETH short 0.1 2100 --tif ALO
    python3 scripts/hl_order.py SOL long 10 90.5 --reduce-only
    python3 scripts/hl_order.py BTC long 0.001 70000 --dry-run
"""
import asyncio
import os
import sys

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.core import VenueConfig, ChainType, VenueTier, Side
from adapters.hyperliquid_adapter import HyperliquidAdapter


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/hl_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|ALO] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (Hyperliquid bare symbols)")
        print("  side:         long or short (buy or sell)")
        print("  size:         quantity in base asset (e.g. 0.001 for BTC)")
        print("  price:        limit price in USD")
        print("  --tif:        time-in-force: GTC (default), IOC, ALO")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show order details + orderbook but don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    side_str = args[1].lower()
    size = float(args[2])
    price = float(args[3])

    if side_str in ("long", "buy", "b"):
        side = Side.LONG
    elif side_str in ("short", "sell", "s"):
        side = Side.SHORT
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

    return symbol, side, size, price, tif, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, side, size, price, tif, reduce_only, dry_run = parse_args(args)

    pk = os.environ.get("HYPERLIQUID_PRIVATE_KEY", os.environ.get("EVM_PRIVATE_KEY", ""))
    if not pk:
        print("ERROR: HYPERLIQUID_PRIVATE_KEY or EVM_PRIVATE_KEY not found in .env")
        sys.exit(1)

    config = VenueConfig(
        name="Hyperliquid", chain="hyperliquid_l1", chain_type=ChainType.EVM,
        settlement_chain="hyperliquid_l1", funding_cycle_hours=1,
        maker_fee_bps=1.5, taker_fee_bps=4.5, max_leverage=50,
        collateral_token="USDC", api_base_url="https://api.hyperliquid.xyz",
        ws_url="wss://api.hyperliquid.xyz/ws", deposit_chain="arbitrum",
        tier=VenueTier.TIER_1, zero_gas=True, symbol_format="{symbol}",
        symbol_overrides={},
    )

    adapter = HyperliquidAdapter(config)
    connected = await adapter.connect(pk)
    if not connected:
        print("ERROR: Failed to connect to Hyperliquid")
        sys.exit(1)

    # Validate symbol exists
    if symbol not in adapter._asset_map:
        print(f"ERROR: Symbol '{symbol}' not found on Hyperliquid")
        print(f"Available: {', '.join(sorted(adapter._asset_map.keys())[:30])}...")
        sys.exit(1)

    # Get current market data for context
    ob = await adapter.get_orderbook(symbol, depth=5)
    fr = await adapter.get_funding_rate(symbol)

    side_label = "BUY/LONG" if side == Side.LONG else "SELL/SHORT"
    notional = size * price

    print("=" * 60)
    print(f"  HYPERLIQUID LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
    print("=" * 60)
    print(f"\n  Symbol:       {symbol}")
    print(f"  Side:         {side_label}")
    print(f"  Size:         {size}")
    print(f"  Price:        ${price:,.4f}")
    print(f"  Notional:     ${notional:,.2f}")
    print(f"  TIF:          {tif}")
    print(f"  Reduce Only:  {reduce_only}")
    print(f"  Wallet:       {adapter._address[:10]}...{adapter._address[-6:]}")

    print(f"\n  --- Market Context ---")
    print(f"  Mark Price:   ${fr.mark_price:,.4f}")
    print(f"  Oracle Price: ${fr.index_price:,.4f}")
    print(f"  Funding:      {fr.rate*100:.6f}%/hr ({fr.annualized_pct:.2f}% ann)")
    if ob.bids and ob.asks:
        print(f"  Best Bid:     ${ob.bids[0].price:,.4f} (sz={ob.bids[0].size})")
        print(f"  Best Ask:     ${ob.asks[0].price:,.4f} (sz={ob.asks[0].size})")
        print(f"  Spread:       {ob.spread_bps:.2f} bps")

        # Distance from market
        mid = (ob.bids[0].price + ob.asks[0].price) / 2
        dist_pct = (price - mid) / mid * 100
        print(f"  Limit vs Mid: {dist_pct:+.3f}%")

    # Get current balance
    raw = await adapter._post_info({"type": "clearinghouseState", "user": adapter._address})
    cross = raw.get("crossMarginSummary", {})
    acct_value = float(cross.get("accountValue", 0))
    margin_used = float(cross.get("totalMarginUsed", 0))
    free_margin = acct_value - margin_used
    print(f"\n  --- Account ---")
    print(f"  NAV:          ${acct_value:,.2f}")
    print(f"  Free Margin:  ${free_margin:,.2f}")

    if dry_run:
        print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
        return

    # Place the order
    print(f"\n  Placing order...")
    result = await adapter.place_limit_order(symbol, side, size, price, reduce_only, tif)

    print(f"\n  --- Result ---")
    print(f"  Status:       {result.status.value}")
    if result.order_id:
        print(f"  Order ID:     {result.order_id}")
    if result.filled_qty > 0:
        print(f"  Filled:       {result.filled_qty} @ ${result.avg_price:,.4f}")
    if result.error:
        print(f"  Error:        {result.error}")

    # Show updated balance after order
    raw2 = await adapter._post_info({"type": "clearinghouseState", "user": adapter._address})
    cross2 = raw2.get("crossMarginSummary", {})
    new_acct = float(cross2.get("accountValue", 0))
    new_margin = float(cross2.get("totalMarginUsed", 0))
    new_free = new_acct - new_margin
    print(f"\n  --- Updated Account ---")
    print(f"  NAV:          ${new_acct:,.2f}")
    print(f"  Free Margin:  ${new_free:,.2f}")

    # Show open orders
    orders = await adapter._post_info({"type": "openOrders", "user": adapter._address})
    symbol_orders = [o for o in orders if o.get("coin") == symbol]
    if symbol_orders:
        print(f"\n  --- Open {symbol} Orders ---")
        for o in symbol_orders:
            s = "BUY" if o["side"] == "B" else "SELL"
            print(f"  {s} {o['sz']} @ ${float(o['limitPx']):,.4f}  oid={o['oid']}")


if __name__ == "__main__":
    asyncio.run(main())
