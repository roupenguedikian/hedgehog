#!/usr/bin/env python3
"""
Hyperliquid market (taker) order tool — used by the /hl-market Claude skill.

Places an IOC limit order at an aggressive price to simulate a market order
with slippage protection.

Usage:
    python3 scripts/hl_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/hl_market_order.py BTC long 0.001
    python3 scripts/hl_market_order.py ETH short 0.5 --slippage-bps 20
    python3 scripts/hl_market_order.py SOL long 10 --reduce-only
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

from models.core import Side, OrderStatus


def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/hl_market_order.py <symbol> <side> <size> [options]")
        print()
        print("  symbol:          BTC, ETH, SOL, etc.")
        print("  side:            long/buy or short/sell")
        print("  size:            quantity in base asset")
        print()
        print("Options:")
        print("  --slippage-bps N  max slippage in basis points (default: 10)")
        print("  --reduce-only    only reduce existing position")
        print("  --dry-run        show what would happen, don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    side_str = args[1].lower()
    size = float(args[2])

    if side_str in ("long", "buy", "b"):
        side = Side.LONG
    elif side_str in ("short", "sell", "s"):
        side = Side.SHORT
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

    return symbol, side, size, slippage_bps, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, side, size, slippage_bps, reduce_only, dry_run = parse_args(args)

    pk = os.environ.get("HYPERLIQUID_PRIVATE_KEY", os.environ.get("EVM_PRIVATE_KEY", ""))
    if not pk:
        print("ERROR: HYPERLIQUID_PRIVATE_KEY or EVM_PRIVATE_KEY not found in .env")
        sys.exit(1)

    from adapters.hyperliquid_adapter import HyperliquidAdapter

    config = {
        "name": "Hyperliquid", "chain": "hyperliquid_l1", "chain_type": "evm",
        "settlement_chain": "hyperliquid_l1", "funding_cycle_hours": 1,
        "maker_fee_bps": 1.5, "taker_fee_bps": 4.5, "max_leverage": 50,
        "collateral_token": "USDC", "api_base_url": "https://api.hyperliquid.xyz",
        "ws_url": "wss://api.hyperliquid.xyz/ws", "deposit_chain": "arbitrum",
        "tier": "tier_1", "zero_gas": True, "symbol_format": "{symbol}",
        "symbol_overrides": {},
    }

    adapter = HyperliquidAdapter(config)
    connected = await adapter.connect(pk)
    if not connected:
        print("ERROR: Failed to connect to Hyperliquid")
        sys.exit(1)

    if symbol not in adapter._asset_map:
        print(f"ERROR: Symbol '{symbol}' not found on Hyperliquid")
        sys.exit(1)

    # Get orderbook and funding
    ob = await adapter.get_orderbook(symbol, depth=5)
    fr = await adapter.get_funding_rate(symbol)

    if not ob.bids or not ob.asks:
        print("ERROR: Empty orderbook")
        sys.exit(1)

    best_bid = ob.bids[0].price
    best_ask = ob.asks[0].price

    # Calculate aggressive price with slippage
    slippage_mult = slippage_bps / 10000
    if side == Side.LONG:
        # Buy: go above best ask
        aggressive_price = best_ask * (1 + slippage_mult)
    else:
        # Sell: go below best bid
        aggressive_price = best_bid * (1 - slippage_mult)

    # Round to reasonable precision
    if aggressive_price > 1000:
        aggressive_price = round(aggressive_price, 2)
    elif aggressive_price > 1:
        aggressive_price = round(aggressive_price, 4)
    else:
        aggressive_price = round(aggressive_price, 6)

    notional = size * aggressive_price
    side_label = "BUY/LONG" if side == Side.LONG else "SELL/SHORT"

    print("=" * 60)
    print(f"  HYPERLIQUID MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
    print("=" * 60)
    print(f"\n  Symbol:        {symbol}")
    print(f"  Side:          {side_label}")
    print(f"  Size:          {size}")
    print(f"  Slippage:      {slippage_bps} bps")
    print(f"  Limit Price:   ${aggressive_price:,.4f} (IOC with slippage cap)")
    print(f"  Max Notional:  ${notional:,.2f}")
    print(f"  Reduce Only:   {reduce_only}")
    print(f"  Wallet:        {adapter._address[:10]}...{adapter._address[-6:]}")

    print(f"\n  --- Market Context ---")
    print(f"  Mark Price:    ${fr.mark_price:,.4f}")
    print(f"  Best Bid:      ${best_bid:,.4f}")
    print(f"  Best Ask:      ${best_ask:,.4f}")
    print(f"  Spread:        {ob.spread_bps:.2f} bps")
    print(f"  Funding:       {fr.rate*100:.6f}%/hr ({fr.annualized_pct:.2f}% ann)")

    # Account balance
    raw = await adapter._post_info({"type": "clearinghouseState", "user": adapter._address})
    cross = raw.get("crossMarginSummary", {})
    acct_value = float(cross.get("accountValue", 0))
    margin_used = float(cross.get("totalMarginUsed", 0))
    print(f"\n  --- Account ---")
    print(f"  NAV:           ${acct_value:,.2f}")
    print(f"  Free Margin:   ${acct_value - margin_used:,.2f}")

    if dry_run:
        print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
        return

    # Place IOC order
    print(f"\n  Placing IOC order...")
    result = await adapter.place_limit_order(symbol, side, size, aggressive_price, reduce_only, "IOC")

    print(f"\n  --- Result ---")
    print(f"  Status:        {result.status.value}")
    if result.order_id:
        print(f"  Order ID:      {result.order_id}")
    if result.filled_qty > 0:
        print(f"  Filled:        {result.filled_qty} @ ${result.avg_price:,.4f}")
        actual_slippage = abs(result.avg_price - (best_ask if side == Side.LONG else best_bid))
        ref = best_ask if side == Side.LONG else best_bid
        actual_bps = actual_slippage / ref * 10000 if ref > 0 else 0
        print(f"  Actual Slip:   {actual_bps:.2f} bps")
    if result.filled_qty < size:
        unfilled = size - result.filled_qty
        print(f"  Unfilled:      {unfilled} (IOC cancelled remainder)")
    if result.error:
        print(f"  Error:         {result.error}")

    # Updated balance
    raw2 = await adapter._post_info({"type": "clearinghouseState", "user": adapter._address})
    cross2 = raw2.get("crossMarginSummary", {})
    new_acct = float(cross2.get("accountValue", 0))
    new_margin = float(cross2.get("totalMarginUsed", 0))
    print(f"\n  --- Updated Account ---")
    print(f"  NAV:           ${new_acct:,.2f}")
    print(f"  Free Margin:   ${new_acct - new_margin:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
