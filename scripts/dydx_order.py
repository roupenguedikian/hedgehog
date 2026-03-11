#!/usr/bin/env python3
"""
dYdX v4 limit order tool — used by the /dydx-limit Claude skill.

Uses the dydx-v4-client Python SDK for order placement on the Cosmos chain.

Usage:
    python3 scripts/dydx_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/dydx_order.py BTC long 0.001 70000
    python3 scripts/dydx_order.py ETH short 0.5 2100 --tif GTX
    python3 scripts/dydx_order.py SOL long 10 90.5 --reduce-only
    python3 scripts/dydx_order.py BTC long 0.001 70000 --dry-run
"""
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

INDEXER_BASE = "https://indexer.dydx.trade/v4"
VALIDATOR_BASE = os.getenv("DYDX_VALIDATOR_URL", "https://dydx-ops-rpc.kingnodes.com")
CHAIN_ID = os.getenv("DYDX_CHAIN_ID", "dydx-mainnet-1")
DEFAULT_ADDRESS = os.getenv("DYDX_WALLET_ADDRESS", "")
MNEMONIC = os.getenv("DYDX_MNEMONIC", "")


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/dydx_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (auto-appended -USD)")
        print("  side:         long or short (buy or sell)")
        print("  size:         quantity in base asset (e.g. 0.001 for BTC)")
        print("  price:        limit price in USD")
        print("  --tif:        time-in-force: GTC (default), IOC, GTX (post-only)")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show order details + market data but don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    if not symbol.endswith("-USD"):
        symbol = symbol + "-USD"
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
            if tif not in ("GTC", "IOC", "GTX"):
                print(f"ERROR: Invalid TIF '{tif}'. Use: GTC, IOC, GTX")
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

    notional = size * price
    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    async with httpx.AsyncClient(base_url=INDEXER_BASE, timeout=20.0) as client:
        # ── Market context ──
        funding_rate = 0.0
        oracle_price = 0.0
        step_size = 0.0
        tick_size = 0.0
        market_id_num = -1

        try:
            resp = await client.get("/perpetualMarkets")
            resp.raise_for_status()
            markets = resp.json().get("markets", {})
            if symbol in markets:
                m = markets[symbol]
                funding_rate = float(m.get("nextFundingRate", 0))
                oracle_price = float(m.get("oraclePrice", 0))
                step_size = float(m.get("stepSize", 0))
                tick_size = float(m.get("tickSize", 0))
                market_id_num = int(m.get("clobPairId", -1))
            else:
                print(f"ERROR: Symbol '{symbol}' not found on dYdX")
                available = [k for k, v in markets.items() if v.get("status") == "ACTIVE"][:20]
                print(f"Available: {', '.join(sorted(available))}...")
                sys.exit(1)
        except Exception as e:
            print(f"  Warning: failed to fetch markets: {e}")

        # Orderbook
        best_bid = 0.0
        best_ask = 0.0
        try:
            resp = await client.get(f"/orderbooks/perpetualMarket/{symbol}")
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids:
                best_bid = float(bids[0].get("price", 0))
            if asks:
                best_ask = float(asks[0].get("price", 0))
        except Exception:
            pass

        ann_rate = funding_rate * 8760  # 1h cycle
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else oracle_price
        spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 and best_bid and best_ask else 0
        dist_pct = (price - mid) / mid * 100 if mid > 0 else 0

        print("=" * 60)
        print(f"  DYDX V4 LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:       {symbol} (clobPairId={market_id_num})")
        print(f"  Side:         {side_label}")
        print(f"  Size:         {size}")
        print(f"  Price:        ${price:,.4f}")
        print(f"  Notional:     ${notional:,.2f}")
        print(f"  TIF:          {tif}")
        print(f"  Reduce Only:  {reduce_only}")
        if step_size:
            print(f"  Step Size:    {step_size}")
        if tick_size:
            print(f"  Tick Size:    {tick_size}")

        print(f"\n  --- Market Context ---")
        print(f"  Oracle Price: ${oracle_price:,.4f}")
        print(f"  Funding:      {funding_rate*100:.6f}%/hr ({ann_rate*100:.2f}% ann)")
        if best_bid and best_ask:
            print(f"  Best Bid:     ${best_bid:,.4f}")
            print(f"  Best Ask:     ${best_ask:,.4f}")
            print(f"  Spread:       {spread_bps:.2f} bps")
            print(f"  Limit vs Mid: {dist_pct:+.3f}%")

        # ── Account balance ──
        if DEFAULT_ADDRESS:
            try:
                resp = await client.get(f"/addresses/{DEFAULT_ADDRESS}")
                resp.raise_for_status()
                acct = resp.json()
                for sa in acct.get("subaccounts", []):
                    if sa.get("subaccountNumber", -1) == 0:
                        equity = float(sa.get("equity", 0))
                        free_coll = float(sa.get("freeCollateral", 0))
                        print(f"\n  --- Account (sub=0) ---")
                        print(f"  NAV:          ${equity:,.2f}")
                        print(f"  Free Margin:  ${free_coll:,.2f}")
                        break
            except Exception as e:
                print(f"\n  --- Account ---")
                print(f"  Error: {e}")
        else:
            print(f"\n  --- Account ---")
            print(f"  [DYDX_WALLET_ADDRESS not set]")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # ── Place order via dydx-v4-client SDK ──
        print(f"\n  Placing order...")

        if not MNEMONIC:
            print("  ERROR: DYDX_MNEMONIC must be set in .env for order placement.")
            print("  This is the 24-word mnemonic that derives your dYdX v4 Cosmos wallet.")
            sys.exit(1)

        try:
            from dydx_v4_client import NodeClient, Wallet
            from dydx_v4_client.node.market import Market
            from dydx_v4_client.indexer.rest.constants import MAINNET_API_URL
            from dydx_v4_client.chain.aerial.config import NetworkConfig
        except ImportError:
            print("  ERROR: dydx-v4-client not installed. Install with:")
            print("    pip install dydx-v4-client")
            sys.exit(1)

        try:
            # Initialize client
            network = NetworkConfig.fetch_mainnet()
            node_client = await NodeClient.connect(network)
            wallet = await Wallet.from_mnemonic(node_client, MNEMONIC, 0)

            # Map TIF
            from dydx_v4_client.node.message import OrderTimeInForce
            tif_map = {
                "GTC": OrderTimeInForce.TIME_IN_FORCE_UNSPECIFIED,
                "IOC": OrderTimeInForce.TIME_IN_FORCE_IOC,
                "GTX": OrderTimeInForce.TIME_IN_FORCE_POST_ONLY,
            }

            # Generate unique client ID
            client_id = int(uuid.uuid4().int % (2**32))

            # Good til block (GTC orders use good_til_block_time instead)
            current_block = await node_client.get_latest_block_height()
            good_til_block = current_block + 20  # ~20 blocks ≈ 20 seconds for short-lived

            # Place long-term order for GTC
            if tif == "GTC":
                # GTC uses good_til_block_time (unix timestamp)
                good_til_time = int(time.time()) + 30 * 24 * 3600  # 30 days

                tx = await node_client.place_order(
                    wallet,
                    wallet.account_number,
                    0,  # subaccount number
                    client_id,
                    market_id_num,
                    2,  # order type: LONG_TERM
                    tif_map.get(tif, OrderTimeInForce.TIME_IN_FORCE_UNSPECIFIED),
                    size,
                    price,
                    good_til_block=0,
                    good_til_block_time=good_til_time,
                    reduce_only=reduce_only,
                    side=1 if is_buy else 2,  # 1=BUY, 2=SELL
                )
            else:
                # Short-term orders (IOC, GTX)
                tx = await node_client.place_order(
                    wallet,
                    wallet.account_number,
                    0,  # subaccount number
                    client_id,
                    market_id_num,
                    0,  # order type: SHORT_TERM
                    tif_map.get(tif, OrderTimeInForce.TIME_IN_FORCE_UNSPECIFIED),
                    size,
                    price,
                    good_til_block=good_til_block,
                    reduce_only=reduce_only,
                    side=1 if is_buy else 2,
                )

            print(f"\n  --- Result ---")
            if tx:
                tx_hash = getattr(tx, "tx_hash", getattr(tx, "hash", str(tx)))
                print(f"  Status:       SUBMITTED")
                print(f"  Tx Hash:      {tx_hash}")
                print(f"  Client ID:    {client_id}")
            else:
                print(f"  Status:       SUBMITTED (no tx response)")

        except Exception as e:
            print(f"\n  --- Result ---")
            print(f"  Error:        {e}")
            return

        # ── Updated account ──
        if DEFAULT_ADDRESS:
            try:
                await asyncio.sleep(2)  # Wait for indexer to update
                resp2 = await client.get(f"/addresses/{DEFAULT_ADDRESS}")
                resp2.raise_for_status()
                acct2 = resp2.json()
                for sa in acct2.get("subaccounts", []):
                    if sa.get("subaccountNumber", -1) == 0:
                        new_equity = float(sa.get("equity", 0))
                        new_free = float(sa.get("freeCollateral", 0))
                        print(f"\n  --- Updated Account ---")
                        print(f"  NAV:          ${new_equity:,.2f}")
                        print(f"  Free Margin:  ${new_free:,.2f}")
                        break
            except Exception:
                pass

            # ── Open orders ──
            try:
                resp3 = await client.get("/orders", params={
                    "address": DEFAULT_ADDRESS,
                    "subaccountNumber": 0,
                    "status": "OPEN",
                    "ticker": symbol,
                    "limit": 10,
                })
                resp3.raise_for_status()
                orders_data = resp3.json()
                order_list = orders_data if isinstance(orders_data, list) else orders_data.get("orders", [])
                if order_list:
                    print(f"\n  --- Open {symbol} Orders ---")
                    for o in order_list:
                        s = o.get("side", "?")
                        print(f"  {s} {o.get('size', '?')} @ ${float(o.get('price', 0)):,.4f}  "
                              f"tif={o.get('timeInForce', '?')}  status={o.get('status', '?')}")
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
