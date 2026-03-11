#!/usr/bin/env python3
"""
dYdX v4 market (taker) order tool — used by the /dydx-market Claude skill.

Places an IOC limit order at an aggressive price to simulate a market order.

Usage:
    python3 scripts/dydx_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/dydx_market_order.py BTC long 0.001
    python3 scripts/dydx_market_order.py ETH short 0.5 --slippage-bps 20
"""
import asyncio
import os
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()

INDEXER_BASE = "https://indexer.dydx.trade/v4"
DEFAULT_ADDRESS = os.getenv("DYDX_WALLET_ADDRESS", "")
MNEMONIC = os.getenv("DYDX_MNEMONIC", "")


def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/dydx_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:          BTC, ETH, SOL, etc. (auto-appended -USD)")
        print("  side:            long/buy or short/sell")
        print("  size:            quantity in base asset")
        print("  --slippage-bps N max slippage in bps (default: 20)")
        print("  --reduce-only    only reduce existing position")
        print("  --dry-run        show what would happen, don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    if not symbol.endswith("-USD"):
        symbol = symbol + "-USD"
    side_str = args[1].lower()
    size = float(args[2])

    if side_str in ("long", "buy", "b"):
        is_buy = True
    elif side_str in ("short", "sell", "s"):
        is_buy = False
    else:
        print(f"ERROR: Invalid side '{side_str}'. Use: long/buy or short/sell")
        sys.exit(1)

    slippage_bps = 20
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

    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    async with httpx.AsyncClient(base_url=INDEXER_BASE, timeout=20.0) as client:
        # Market context
        oracle_price = 0.0
        funding_rate = 0.0
        market_id_num = -1
        best_bid = 0.0
        best_ask = 0.0

        try:
            resp = await client.get("/perpetualMarkets")
            resp.raise_for_status()
            markets = resp.json().get("markets", {})
            if symbol in markets:
                m = markets[symbol]
                oracle_price = float(m.get("oraclePrice", 0))
                funding_rate = float(m.get("nextFundingRate", 0))
                market_id_num = int(m.get("clobPairId", -1))
            else:
                print(f"ERROR: Symbol '{symbol}' not found on dYdX")
                sys.exit(1)
        except Exception as e:
            print(f"  Warning: failed to fetch markets: {e}")

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

        ref_price = best_ask if is_buy else best_bid
        if ref_price == 0:
            ref_price = oracle_price

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
        print(f"  DYDX V4 MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:        {symbol}")
        print(f"  Side:          {side_label}")
        print(f"  Size:          {size}")
        print(f"  Slippage Cap:  {slippage_bps} bps")
        print(f"  Limit Price:   ${aggressive_price:,.4f} (IOC)")
        print(f"  Est. Notional: ${est_notional:,.2f}")
        print(f"  Reduce Only:   {reduce_only}")

        print(f"\n  --- Market Context ---")
        print(f"  Oracle Price:  ${oracle_price:,.4f}")
        print(f"  Best Bid:      ${best_bid:,.4f}")
        print(f"  Best Ask:      ${best_ask:,.4f}")
        print(f"  Spread:        {spread_bps:.2f} bps")
        print(f"  Funding:       {funding_rate*100:.6f}%/hr ({funding_rate*8760*100:.2f}% ann)")

        # Account
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
                        print(f"  NAV:           ${equity:,.2f}")
                        print(f"  Free Margin:   ${free_coll:,.2f}")
                        break
            except Exception as e:
                print(f"\n  --- Account ---")
                print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # Place IOC order
        print(f"\n  Placing IOC order...")

        if not MNEMONIC:
            print("  ERROR: DYDX_MNEMONIC must be set in .env")
            sys.exit(1)

        try:
            from dydx_v4_client import NodeClient, Wallet
            from dydx_v4_client.chain.aerial.config import NetworkConfig
            from dydx_v4_client.node.message import OrderTimeInForce
        except ImportError:
            print("  ERROR: dydx-v4-client not installed. Install with: pip install dydx-v4-client")
            sys.exit(1)

        try:
            network = NetworkConfig.fetch_mainnet()
            node_client = await NodeClient.connect(network)
            wallet = await Wallet.from_mnemonic(node_client, MNEMONIC, 0)

            client_id = int(uuid.uuid4().int % (2**32))
            current_block = await node_client.get_latest_block_height()
            good_til_block = current_block + 10

            tx = await node_client.place_order(
                wallet,
                wallet.account_number,
                0,
                client_id,
                market_id_num,
                0,  # SHORT_TERM
                OrderTimeInForce.TIME_IN_FORCE_IOC,
                size,
                aggressive_price,
                good_til_block=good_til_block,
                reduce_only=reduce_only,
                side=1 if is_buy else 2,
            )

            print(f"\n  --- Result ---")
            if tx:
                tx_hash = getattr(tx, "tx_hash", getattr(tx, "hash", str(tx)))
                print(f"  Status:        SUBMITTED (IOC)")
                print(f"  Tx Hash:       {tx_hash}")
            else:
                print(f"  Status:        SUBMITTED")

        except Exception as e:
            print(f"\n  --- Result ---")
            print(f"  Error:         {e}")
            return

        # Updated account
        if DEFAULT_ADDRESS:
            try:
                await asyncio.sleep(2)
                resp2 = await client.get(f"/addresses/{DEFAULT_ADDRESS}")
                resp2.raise_for_status()
                acct2 = resp2.json()
                for sa in acct2.get("subaccounts", []):
                    if sa.get("subaccountNumber", -1) == 0:
                        print(f"\n  --- Updated Account ---")
                        print(f"  NAV:           ${float(sa.get('equity', 0)):,.2f}")
                        print(f"  Free Margin:   ${float(sa.get('freeCollateral', 0)):,.2f}")
                        break
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
