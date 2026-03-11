#!/usr/bin/env python3
"""
Ethereal DEX limit order tool — used by the /ethereal-limit Claude skill.

Uses the official ethereal-sdk for EIP-712 signing and order submission.
Market context fetched via raw REST (same patterns as connectors/ethereal_query.py).

Usage:
    python3 scripts/ethereal_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|FOK] [--post-only] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/ethereal_order.py BTC long 0.01 80000
    python3 scripts/ethereal_order.py ETH short 1 2500 --post-only
    python3 scripts/ethereal_order.py SOL long 10 120 --tif IOC
    python3 scripts/ethereal_order.py BTC long 0.01 80000 --dry-run
"""
import asyncio
import os
import sys
from decimal import Decimal

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

API_BASE = "https://api.ethereal.trade"
RPC_URL = "https://rpc.ethereal.trade"
PRIVATE_KEY = os.environ.get("ETHEREAL_PRIVATE_KEY", os.environ.get("EVM_PRIVATE_KEY", ""))
WALLET_ADDRESS = os.environ.get("ETHEREAL_WALLET_ADDRESS", "")


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/ethereal_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|FOK] [--post-only] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (USD auto-appended → BTCUSD)")
        print("  side:         long or short (buy or sell)")
        print("  size:         quantity in base asset (e.g. 0.01 for BTC)")
        print("  price:        limit price in USD")
        print("  --tif:        time-in-force: GTC (default), IOC, FOK")
        print("  --post-only:  maker only, rejected if would cross")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show order details + market data but don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    # Strip trailing -USD or USD if user passes full format
    if symbol.endswith("-USD"):
        symbol = symbol[:-4]
    elif symbol.endswith("USD") and len(symbol) > 3:
        symbol = symbol[:-3]
    # Internal ticker for SDK: BTCUSD
    ticker = symbol + "USD"
    # Display ticker: BTC-USD
    display = symbol + "-USD"

    side_str = args[1].lower()
    size = float(args[2])
    price = float(args[3])

    if side_str in ("long", "buy", "b"):
        side = 0  # BUY
    elif side_str in ("short", "sell", "s"):
        side = 1  # SELL
    else:
        print(f"ERROR: Invalid side '{side_str}'. Use: long/buy/b or short/sell/s")
        sys.exit(1)

    tif = "GTC"
    post_only = False
    reduce_only = False
    dry_run = False

    i = 4
    while i < len(args):
        if args[i] == "--tif" and i + 1 < len(args):
            tif = args[i + 1].upper()
            if tif not in ("GTC", "IOC", "FOK"):
                print(f"ERROR: Invalid TIF '{tif}'. Use: GTC, IOC, FOK")
                sys.exit(1)
            i += 2
        elif args[i] == "--post-only":
            post_only = True
            i += 1
        elif args[i] == "--reduce-only":
            reduce_only = True
            i += 1
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"ERROR: Unknown flag '{args[i]}'")
            sys.exit(1)

    return symbol, ticker, display, side, size, price, tif, post_only, reduce_only, dry_run


async def fetch_market_context(symbol_display, wallet_address):
    """Fetch funding rate, orderbook, and account balance via raw REST."""
    ctx = {
        "funding_1h": 0.0, "mark": 0.0, "oracle": 0.0,
        "best_bid": 0.0, "best_ask": 0.0, "bid_sz": 0.0, "ask_sz": 0.0,
        "nav": 0.0, "free_margin": 0.0, "product_id": "",
    }

    async with httpx.AsyncClient(base_url=API_BASE, timeout=15.0) as client:
        # Products + funding rates
        try:
            resp = await client.get("/v1/product", params={"limit": 100})
            resp.raise_for_status()
            products = resp.json().get("data", [])
            for p in products:
                if p.get("displayTicker") == symbol_display:
                    ctx["funding_1h"] = float(p.get("fundingRate1h") or 0)
                    ctx["product_id"] = p.get("id", "")
                    break
        except Exception as e:
            print(f"  Warning: failed to fetch products: {e}")

        # Prices (mark, oracle, bid, ask)
        if ctx["product_id"]:
            try:
                resp = await client.get("/v1/product/market-price",
                                        params={"productIds": [ctx["product_id"]]})
                resp.raise_for_status()
                for mp in resp.json().get("data", []):
                    if mp.get("productId") == ctx["product_id"]:
                        ctx["oracle"] = float(mp.get("oraclePrice") or 0)
                        ctx["best_bid"] = float(mp.get("bestBidPrice") or 0)
                        ctx["best_ask"] = float(mp.get("bestAskPrice") or 0)
                        ctx["mark"] = (ctx["best_bid"] + ctx["best_ask"]) / 2 if ctx["best_bid"] else ctx["oracle"]
                        ctx["bid_sz"] = float(mp.get("bestBidQuantity") or 0)
                        ctx["ask_sz"] = float(mp.get("bestAskQuantity") or 0)
            except Exception as e:
                print(f"  Warning: failed to fetch prices: {e}")

        # Account balance
        if wallet_address:
            try:
                resp = await client.get("/v1/subaccount",
                                        params={"sender": wallet_address, "limit": 10})
                resp.raise_for_status()
                subaccounts = resp.json().get("data", [])
                if subaccounts:
                    sa_id = subaccounts[0]["id"]
                    bal_resp = await client.get("/v1/subaccount/balance",
                                                params={"subaccountId": sa_id, "limit": 20})
                    bal_resp.raise_for_status()
                    for b in bal_resp.json().get("data", []):
                        ctx["nav"] += float(b.get("amount") or 0)
                        ctx["free_margin"] += float(b.get("available") or 0)
            except Exception as e:
                print(f"  Warning: failed to fetch account: {e}")

    return ctx


async def main():
    args = sys.argv[1:]
    symbol, ticker, display, side, size, price, tif, post_only, reduce_only, dry_run = parse_args(args)

    if not PRIVATE_KEY:
        print("ERROR: ETHEREAL_PRIVATE_KEY or EVM_PRIVATE_KEY must be set in .env")
        sys.exit(1)

    side_label = "BUY/LONG" if side == 0 else "SELL/SHORT"
    notional = size * price

    # Fetch market context via raw REST
    ctx = await fetch_market_context(display, WALLET_ADDRESS)

    ann_rate = ctx["funding_1h"] * 8760
    mid = (ctx["best_bid"] + ctx["best_ask"]) / 2 if ctx["best_bid"] and ctx["best_ask"] else ctx["mark"]
    spread_bps = (ctx["best_ask"] - ctx["best_bid"]) / mid * 10000 if mid > 0 and ctx["best_bid"] and ctx["best_ask"] else 0
    dist_pct = (price - mid) / mid * 100 if mid > 0 else 0

    # Map TIF for display: GTC is the default maker-friendly option
    tif_display = tif
    if post_only:
        tif_display = f"{tif} + POST-ONLY"

    # Derive wallet address from private key for display
    wallet_display = WALLET_ADDRESS
    if not wallet_display and PRIVATE_KEY:
        try:
            from eth_account import Account
            acct = Account.from_key(PRIVATE_KEY)
            wallet_display = acct.address
        except Exception:
            wallet_display = "unknown"

    print("=" * 60)
    print(f"  ETHEREAL LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
    print("=" * 60)
    print(f"\n  Symbol:       {display}")
    print(f"  Side:         {side_label}")
    print(f"  Size:         {size}")
    print(f"  Price:        ${price:,.4f}")
    print(f"  Notional:     ${notional:,.2f}")
    print(f"  TIF:          {tif_display}")
    print(f"  Reduce Only:  {reduce_only}")
    if wallet_display:
        print(f"  Wallet:       {wallet_display[:10]}...{wallet_display[-6:]}")

    print(f"\n  --- Market Context ---")
    print(f"  Mark Price:   ${ctx['mark']:,.4f}")
    print(f"  Oracle Price: ${ctx['oracle']:,.4f}")
    print(f"  Funding:      {ctx['funding_1h']*100:.4f}%/1h ({ann_rate*100:+.2f}% ann)")
    if ctx["best_bid"] and ctx["best_ask"]:
        print(f"  Best Bid:     ${ctx['best_bid']:,.4f} (sz={ctx['bid_sz']:.4f})")
        print(f"  Best Ask:     ${ctx['best_ask']:,.4f} (sz={ctx['ask_sz']:.4f})")
        print(f"  Spread:       {spread_bps:.2f} bps")
        print(f"  Limit vs Mid: {dist_pct:+.3f}%")

    print(f"\n  --- Account ---")
    print(f"  NAV:          ${ctx['nav']:,.2f}")
    print(f"  Free Margin:  ${ctx['free_margin']:,.2f}")

    if dry_run:
        print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
        return

    # ── Place order via ethereal-sdk ──
    from ethereal import AsyncRESTClient

    config = {
        "base_url": API_BASE,
        "chain_config": {
            "rpc_url": RPC_URL,
            "private_key": PRIVATE_KEY,
        },
    }

    sdk = await AsyncRESTClient.create(config)

    try:
        # SDK maps GTC → GTD internally
        print(f"\n  Placing order...")
        result = await sdk.create_order(
            order_type="LIMIT",
            quantity=Decimal(str(size)),
            side=side,
            price=Decimal(str(price)),
            ticker=ticker,
            time_in_force=tif,
            post_only=post_only,
            reduce_only=reduce_only,
        )

        print(f"\n  --- Result ---")
        order_id = getattr(result, "id", None) or "N/A"
        filled = getattr(result, "filled", None)
        result_code = getattr(result, "result", None)
        print(f"  Status:       {result_code or 'SUBMITTED'}")
        print(f"  Order ID:     {order_id}")
        if filled and float(filled) > 0:
            print(f"  Filled:       {filled}")

        if result_code and str(result_code) != "Ok":
            print(f"  [!] Result: {result_code}")

        # Updated account balance
        ctx2 = await fetch_market_context(display, WALLET_ADDRESS)
        print(f"\n  --- Updated Account ---")
        print(f"  NAV:          ${ctx2['nav']:,.2f}")
        print(f"  Free Margin:  ${ctx2['free_margin']:,.2f}")

        # Show open orders for this symbol
        try:
            sa_resp = await sdk._http.get(f"/v1/subaccount", params={"sender": WALLET_ADDRESS, "limit": 1})
            if sa_resp and isinstance(sa_resp, dict):
                sa_data = sa_resp.get("data", [])
            else:
                sa_data = []
            if sa_data:
                sa_id = sa_data[0]["id"]
                open_orders = await sdk.list_orders(
                    subaccount_id=sa_id,
                    is_working=True,
                    limit=20,
                )
                symbol_orders = [o for o in open_orders
                                 if getattr(o, "display_ticker", None) == display
                                 or getattr(o, "product_id", None) == ctx["product_id"]]
                if symbol_orders:
                    print(f"\n  --- Open {display} Orders ---")
                    for o in symbol_orders:
                        s = "BUY" if getattr(o, "side", 0) == 0 else "SELL"
                        px = getattr(o, "price", 0)
                        qty = getattr(o, "quantity", 0)
                        oid = getattr(o, "id", "?")
                        status = getattr(o, "status", "?")
                        print(f"  {s} {qty} @ ${float(px):,.4f}  id={str(oid)[:8]}  status={status}")
        except Exception:
            pass

    except Exception as e:
        print(f"\n  --- Error ---")
        print(f"  {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        await sdk.close()


if __name__ == "__main__":
    asyncio.run(main())
