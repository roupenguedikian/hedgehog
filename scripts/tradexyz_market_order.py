#!/usr/bin/env python3
"""
trade.xyz (XYZ) HIP-3 market (taker) order tool — used by the /tradexyz-market Claude skill.

Places an IOC limit order at an aggressive price to simulate a market order
with slippage protection. XYZ is a HIP-3 builder DEX on Hyperliquid — orders
are placed via the HL SDK with `xyz:SYMBOL` coin names; data is fetched via
the HL info API with `dex: "xyz"`.

Usage:
    python3 scripts/orders/tradexyz_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/orders/tradexyz_market_order.py NVDA long 10
    python3 scripts/orders/tradexyz_market_order.py BTC short 0.001 --slippage-bps 20
    python3 scripts/orders/tradexyz_market_order.py TSLA long 5 --reduce-only
"""
import os
import sys

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

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
load_env(os.path.join(_ROOT, ".env"))

API = "https://api.hyperliquid.xyz"
XYZ_DEX = "xyz"


def _post(payload: dict) -> dict | list:
    """Synchronous POST to Hyperliquid info API."""
    resp = httpx.post(f"{API}/info", json=payload, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/orders/tradexyz_market_order.py <symbol> <side> <size> [options]")
        print()
        print("  symbol:          NVDA, BTC, ETH, TSLA, etc. (bare symbols)")
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

    is_buy = side_str in ("long", "buy", "b")
    if not is_buy and side_str not in ("short", "sell", "s"):
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


def main():
    args = sys.argv[1:]
    symbol, is_buy, size, slippage_bps, reduce_only, dry_run = parse_args(args)

    pk = os.environ.get("HYPERLIQUID_PRIVATE_KEY", os.environ.get("EVM_PRIVATE_KEY", ""))
    if not pk:
        print("ERROR: HYPERLIQUID_PRIVATE_KEY or EVM_PRIVATE_KEY not found in .env")
        sys.exit(1)

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    wallet = Account.from_key(pk)
    address = wallet.address

    # ── Fetch XYZ universe via dex-aware info API ──
    meta = _post({"type": "metaAndAssetCtxs", "dex": XYZ_DEX})
    universe = meta[0]["universe"]
    ctxs = meta[1]

    # Build symbol maps — coins are 'xyz:NVDA' format
    asset_map = {}     # clean_name -> index
    raw_names = {}     # clean_name -> 'xyz:NVDA'
    sz_decimals = {}   # clean_name -> szDecimals
    for i, u in enumerate(universe):
        raw = u["name"]
        clean = raw.split(":", 1)[1] if ":" in raw else raw
        asset_map[clean] = i
        raw_names[clean] = raw
        sz_decimals[clean] = u.get("szDecimals", 3)

    if symbol not in asset_map:
        print(f"ERROR: Symbol '{symbol}' not found on trade.xyz")
        avail = sorted(asset_map.keys())
        print(f"  Available: {', '.join(avail[:30])}{'...' if len(avail) > 30 else ''}")
        sys.exit(1)

    coin = raw_names[symbol]  # e.g. 'xyz:NVDA'
    idx = asset_map[symbol]
    ctx = ctxs[idx]

    # Market data from asset context
    funding_rate = float(ctx.get("funding") or 0)
    mark_price = float(ctx.get("markPx") or 0)
    oracle_price = float(ctx.get("oraclePx") or 0)

    # ── Get orderbook ──
    ob = _post({"type": "l2Book", "coin": coin})
    levels = ob.get("levels", [[], []])
    bids = levels[0] if len(levels) > 0 else []
    asks = levels[1] if len(levels) > 1 else []

    if not bids or not asks:
        print("ERROR: Empty orderbook for", symbol)
        sys.exit(1)

    best_bid = float(bids[0]["px"])
    best_ask = float(asks[0]["px"])
    spread_bps = (best_ask - best_bid) / best_bid * 10000 if best_bid > 0 else 0

    # ── Calculate aggressive IOC price with slippage ──
    slippage_mult = slippage_bps / 10000
    if is_buy:
        aggressive_price = best_ask * (1 + slippage_mult)
    else:
        aggressive_price = best_bid * (1 - slippage_mult)

    # Round to tick size (significant figures based on price magnitude)
    if aggressive_price > 1000:
        aggressive_price = round(aggressive_price, 2)
    elif aggressive_price > 1:
        aggressive_price = round(aggressive_price, 4)
    else:
        aggressive_price = round(aggressive_price, 6)

    # Round size to sz_decimals
    sd = sz_decimals.get(symbol, 3)
    size = round(size, sd)

    notional = size * aggressive_price
    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    print("=" * 60)
    print(f"  TRADE.XYZ MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
    print("=" * 60)
    print(f"\n  Symbol:        {symbol} ({coin})")
    print(f"  Side:          {side_label}")
    print(f"  Size:          {size}")
    print(f"  Slippage:      {slippage_bps} bps")
    print(f"  Limit Price:   ${aggressive_price:,.4f} (IOC with slippage cap)")
    print(f"  Max Notional:  ${notional:,.2f}")
    print(f"  Reduce Only:   {reduce_only}")
    print(f"  Wallet:        {address[:10]}...{address[-6:]}")

    print(f"\n  --- Market Context ---")
    print(f"  Mark Price:    ${mark_price:,.4f}")
    print(f"  Oracle Price:  ${oracle_price:,.4f}")
    print(f"  Best Bid:      ${best_bid:,.4f}")
    print(f"  Best Ask:      ${best_ask:,.4f}")
    print(f"  Spread:        {spread_bps:.2f} bps")
    print(f"  Funding:       {funding_rate*100:.6f}%/hr ({funding_rate*8760*100:.2f}% ann)")

    # ── Account balance (XYZ clearinghouse) ──
    try:
        user = _post({"type": "clearinghouseState", "user": address, "dex": XYZ_DEX})
        cross = user.get("crossMarginSummary", {})
        acct_value = float(cross.get("accountValue", 0))
        margin_used = float(cross.get("totalMarginUsed", 0))
        print(f"\n  --- Account (XYZ clearinghouse) ---")
        print(f"  NAV:           ${acct_value:,.2f}")
        print(f"  Free Margin:   ${acct_value - margin_used:,.2f}")
    except Exception as e:
        print(f"\n  --- Account ---")
        print(f"  Error: {e}")

    if dry_run:
        print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
        return

    # ── Place IOC order via HL SDK ──
    print(f"\n  Placing IOC order...")
    info = Info(base_url=API, skip_ws=True)
    exchange = Exchange(wallet, base_url=API)

    order_type = {"limit": {"tif": "Ioc"}}
    result = exchange.order(coin, is_buy, size, aggressive_price, order_type,
                            reduce_only=reduce_only)

    # Parse result
    status = result.get("status", "unknown")
    response = result.get("response", {})

    print(f"\n  --- Result ---")
    print(f"  Status:        {status}")

    if status == "ok":
        data = response.get("data", {})
        statuses = data.get("statuses", [])
        for s in statuses:
            if "filled" in s:
                filled = s["filled"]
                print(f"  Filled:        {filled.get('totalSz', '?')} @ ${float(filled.get('avgPx', 0)):,.4f}")
                avg_px = float(filled.get("avgPx", 0))
                ref = best_ask if is_buy else best_bid
                actual_bps = abs(avg_px - ref) / ref * 10000 if ref > 0 else 0
                print(f"  Actual Slip:   {actual_bps:.2f} bps")
            elif "resting" in s:
                print(f"  Resting:       oid={s['resting'].get('oid', '?')}")
            elif "error" in s:
                print(f"  Error:         {s['error']}")
                sys.exit(1)
    else:
        print(f"  Error:         {result}")
        sys.exit(1)

    # ── Updated balance ──
    try:
        user2 = _post({"type": "clearinghouseState", "user": address, "dex": XYZ_DEX})
        cross2 = user2.get("crossMarginSummary", {})
        new_acct = float(cross2.get("accountValue", 0))
        new_margin = float(cross2.get("totalMarginUsed", 0))
        print(f"\n  --- Updated Account ---")
        print(f"  NAV:           ${new_acct:,.2f}")
        print(f"  Free Margin:   ${new_acct - new_margin:,.2f}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
