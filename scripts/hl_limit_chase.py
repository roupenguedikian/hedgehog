#!/usr/bin/env python3
"""
Hyperliquid limit chase — continuously re-prices to stay at best bid/ask.

Usage:
    python3 scripts/hl_limit_chase.py <symbol> <side> <size> [--reduce-only] [--max-iterations N] [--interval S] [--dry-run]

Logic:
  - LONG/BUY:  places limit at best bid, re-prices every interval to stay best bid
  - SHORT/SELL: places limit at best ask, re-prices every interval to stay best ask
  - Uses ALO (Add Liquidity Only) to guarantee maker fills — no crossing
  - Cancels and re-places if the order is no longer at the top of book
  - Stops when fully filled or max iterations reached

Examples:
    python3 scripts/hl_limit_chase.py BTC long 0.001 --dry-run
    python3 scripts/hl_limit_chase.py ETH short 0.5 --reduce-only
    python3 scripts/hl_limit_chase.py SOL long 10 --interval 3 --max-iterations 50
"""
import asyncio
import os
import sys
import time

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
from adapters.hyperliquid_adapter import HyperliquidAdapter


def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/hl_limit_chase.py <symbol> <side> <size> [options]")
        print()
        print("  symbol:            BTC, ETH, SOL, etc.")
        print("  side:              long/buy or short/sell")
        print("  size:              quantity in base asset")
        print()
        print("Options:")
        print("  --reduce-only      only reduce existing position")
        print("  --max-iterations N max re-price attempts (default: 100)")
        print("  --interval S       seconds between checks (default: 2)")
        print("  --dry-run          show what would happen, don't place orders")
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

    max_iterations = 100
    interval = 2.0
    reduce_only = False
    dry_run = False

    i = 3
    while i < len(args):
        if args[i] == "--max-iterations" and i + 1 < len(args):
            max_iterations = int(args[i + 1])
            i += 2
        elif args[i] == "--interval" and i + 1 < len(args):
            interval = float(args[i + 1])
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

    return symbol, side, size, max_iterations, interval, reduce_only, dry_run


async def main():
    args = sys.argv[1:]
    symbol, side, size, max_iterations, interval, reduce_only, dry_run = parse_args(args)

    pk = os.environ.get("HYPERLIQUID_PRIVATE_KEY", os.environ.get("EVM_PRIVATE_KEY", ""))
    if not pk:
        print("ERROR: HYPERLIQUID_PRIVATE_KEY or EVM_PRIVATE_KEY not found in .env")
        sys.exit(1)

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

    side_label = "BUY/LONG (chasing best bid)" if side == Side.LONG else "SELL/SHORT (chasing best ask)"

    # Show initial state
    ob = await adapter.get_orderbook(symbol, depth=5)
    best_bid = ob.bids[0].price if ob.bids else 0
    best_ask = ob.asks[0].price if ob.asks else 0
    target_price = best_bid if side == Side.LONG else best_ask

    print("=" * 60)
    print(f"  HYPERLIQUID LIMIT CHASE {'(DRY RUN)' if dry_run else ''}")
    print("=" * 60)
    print(f"\n  Symbol:         {symbol}")
    print(f"  Side:           {side_label}")
    print(f"  Size:           {size}")
    print(f"  Initial Price:  ${target_price:,.4f}")
    print(f"  Notional:       ${size * target_price:,.2f}")
    print(f"  TIF:            ALO (maker only)")
    print(f"  Reduce Only:    {reduce_only}")
    print(f"  Max Iterations: {max_iterations}")
    print(f"  Check Interval: {interval}s")
    print(f"  Wallet:         {adapter._address[:10]}...{adapter._address[-6:]}")
    print(f"\n  Best Bid: ${best_bid:,.4f}  |  Best Ask: ${best_ask:,.4f}  |  Spread: {ob.spread_bps:.2f} bps")

    raw = await adapter._post_info({"type": "clearinghouseState", "user": adapter._address})
    cross = raw.get("crossMarginSummary", {})
    acct_value = float(cross.get("accountValue", 0))
    margin_used = float(cross.get("totalMarginUsed", 0))
    print(f"  NAV: ${acct_value:,.2f}  |  Free Margin: ${acct_value - margin_used:,.2f}")

    if dry_run:
        print(f"\n  [DRY RUN] Chase NOT started. Remove --dry-run to execute.")
        return

    # ── Chase loop ──
    remaining = size
    total_filled = 0.0
    total_cost = 0.0
    current_oid = None
    current_price = None

    print(f"\n  Starting chase loop...")
    print(f"  {'ITER':>4s} | {'ACTION':>10s} | {'PRICE':>12s} | {'FILLED':>10s} | {'REMAINING':>10s} | {'BID':>12s} | {'ASK':>12s}")
    print("  " + "-" * 80)

    for iteration in range(1, max_iterations + 1):
        try:
            # Get current orderbook
            ob = await adapter.get_orderbook(symbol, depth=3)
            best_bid = ob.bids[0].price if ob.bids else 0
            best_ask = ob.asks[0].price if ob.asks else 0
            target = best_bid if side == Side.LONG else best_ask

            # Check if we need to re-price
            need_new_order = False

            if current_oid is None:
                need_new_order = True
                action = "PLACE"
            elif current_price != target:
                # Cancel old order and check fill
                cancelled = await adapter.cancel_order(symbol, current_oid)
                # Check how much was filled before cancel
                action = "REPRICE"
                need_new_order = True
                current_oid = None
            else:
                action = "HOLD"

            if need_new_order and remaining > 0:
                result = await adapter.place_limit_order(
                    symbol, side, remaining, target,
                    reduce_only=reduce_only, tif="ALO"
                )

                if result.status == OrderStatus.FILLED:
                    total_filled += result.filled_qty
                    total_cost += result.filled_qty * result.avg_price
                    remaining -= result.filled_qty
                    current_oid = None
                    action = "FILLED"
                elif result.status == OrderStatus.SUBMITTED:
                    current_oid = result.order_id
                    current_price = target
                    if result.filled_qty > 0:
                        total_filled += result.filled_qty
                        total_cost += result.filled_qty * result.avg_price
                        remaining -= result.filled_qty
                        action = "PARTIAL"
                elif result.status == OrderStatus.FAILED:
                    # ALO rejected (would cross) — wait and retry
                    action = "REJECTED"
                    current_oid = None

            print(f"  {iteration:>4d} | {action:>10s} | ${target:>11,.4f} | "
                  f"{total_filled:>10.4f} | {remaining:>10.4f} | "
                  f"${best_bid:>11,.4f} | ${best_ask:>11,.4f}")

            if remaining <= 0:
                print(f"\n  FULLY FILLED!")
                break

            # Check remaining orders to see if our order got filled in the meantime
            if current_oid and action == "HOLD":
                orders = await adapter._post_info({"type": "openOrders", "user": adapter._address})
                our_order = [o for o in orders if str(o.get("oid")) == current_oid]
                if not our_order:
                    # Order gone — either filled or cancelled externally
                    current_oid = None
                    # Recalculate remaining from position
                    positions = await adapter.get_positions()
                    for p in positions:
                        if p.symbol == symbol:
                            actual_size = p.size
                            if (side == Side.LONG and p.side == Side.LONG) or \
                               (side == Side.SHORT and p.side == Side.SHORT):
                                total_filled = actual_size
                                remaining = size - total_filled
                                if remaining <= 0:
                                    print(f"\n  FULLY FILLED (detected from position)!")
                                    break

        except KeyboardInterrupt:
            print(f"\n  Chase interrupted by user.")
            if current_oid:
                print(f"  Cancelling outstanding order {current_oid}...")
                await adapter.cancel_order(symbol, current_oid)
            break
        except Exception as e:
            print(f"  {iteration:>4d} | {'ERROR':>10s} | {str(e)[:50]}")

        await asyncio.sleep(interval)

    # ── Summary ──
    avg_price = total_cost / total_filled if total_filled > 0 else 0
    print(f"\n  === CHASE SUMMARY ===")
    print(f"  Total Filled:  {total_filled:.4f} / {size:.4f}")
    print(f"  Avg Price:     ${avg_price:,.4f}")
    print(f"  Total Cost:    ${total_cost:,.2f}")
    print(f"  Iterations:    {iteration}")

    # Cancel any remaining order
    if current_oid:
        print(f"  Cancelling resting order {current_oid}...")
        await adapter.cancel_order(symbol, current_oid)

    # Final account state
    raw2 = await adapter._post_info({"type": "clearinghouseState", "user": adapter._address})
    cross2 = raw2.get("crossMarginSummary", {})
    new_acct = float(cross2.get("accountValue", 0))
    new_margin = float(cross2.get("totalMarginUsed", 0))
    print(f"  NAV:           ${new_acct:,.2f}")
    print(f"  Free Margin:   ${new_acct - new_margin:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
