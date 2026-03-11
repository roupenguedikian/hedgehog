#!/usr/bin/env python3
"""
Drift Protocol limit order tool — used by the /drift-limit Claude skill.

Uses the Drift Gateway REST API for order placement (avoids heavy Solana SDK dependency).
Drift Gateway must be running locally or accessible at DRIFT_GATEWAY_URL.

Fallback: uses driftpy SDK if gateway is not available.

Usage:
    python3 scripts/drift_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/drift_order.py BTC long 0.001 70000
    python3 scripts/drift_order.py ETH short 0.5 2100 --tif GTX
    python3 scripts/drift_order.py SOL long 10 90.5 --reduce-only
    python3 scripts/drift_order.py BTC long 0.001 70000 --dry-run
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

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

DATA_API = "https://data.api.drift.trade"
DLOB_API = "https://dlob.drift.trade"
GATEWAY_URL = os.getenv("DRIFT_GATEWAY_URL", "http://localhost:8080")
DEFAULT_ADDRESS = os.getenv("DRIFT_WALLET_ADDRESS", "")
DRIFT_PRIVATE_KEY = os.getenv("DRIFT_PRIVATE_KEY", os.getenv("SOLANA_PRIVATE_KEY", ""))


# Market index mapping (Drift perp market indices)
MARKET_INDICES = {
    "SOL-PERP": 0, "BTC-PERP": 1, "ETH-PERP": 2, "APT-PERP": 3,
    "MATIC-PERP": 4, "ARB-PERP": 5, "DOGE-PERP": 6, "BNB-PERP": 7,
    "SUI-PERP": 8, "1MPEPE-PERP": 9, "OP-PERP": 10, "AVAX-PERP": 11,
    "LINK-PERP": 12, "WIF-PERP": 14, "JTO-PERP": 15, "SEI-PERP": 16,
    "BONK-PERP": 17, "JUP-PERP": 20, "TIA-PERP": 21, "NEAR-PERP": 22,
    "ADA-PERP": 23, "XRP-PERP": 24, "TRX-PERP": 25, "DOT-PERP": 26,
    "HYPE-PERP": 35, "TRUMP-PERP": 38,
}


async def resolve_account_id(client, authority):
    resp = await client.get(f"{DATA_API}/authority/{authority}/accounts")
    resp.raise_for_status()
    data = resp.json()
    accounts = data.get("accounts", [])
    if accounts:
        return accounts[0]["accountId"]
    return ""


def parse_args(args):
    if len(args) < 4:
        print("Usage: python3 scripts/drift_order.py <symbol> <side> <size> <price> [--tif GTC|IOC|GTX] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:       BTC, ETH, SOL, etc. (auto-appended -PERP)")
        print("  side:         long or short (buy or sell)")
        print("  size:         quantity in base asset (e.g. 0.001 for BTC)")
        print("  price:        limit price in USD")
        print("  --tif:        time-in-force: GTC (default), IOC, GTX (post-only)")
        print("  --reduce-only: only reduce existing position")
        print("  --dry-run:    show order details + market data but don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    if not symbol.endswith("-PERP"):
        symbol = symbol + "-PERP"
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

    if symbol not in MARKET_INDICES:
        print(f"ERROR: Symbol '{symbol}' not found. Available: {', '.join(sorted(MARKET_INDICES.keys()))}")
        sys.exit(1)

    market_index = MARKET_INDICES[symbol]
    notional = size * price
    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    async with httpx.AsyncClient(timeout=20.0) as client:
        # ── Market context ──
        oracle_price = 0.0
        mark_price = 0.0
        funding_pct = 0.0

        try:
            resp = await client.get(f"{DATA_API}/stats/markets")
            resp.raise_for_status()
            all_markets = resp.json().get("markets", [])
            for m in all_markets:
                if m.get("symbol") == symbol:
                    oracle_price = float(m.get("oraclePrice", 0))
                    mark_price = float(m.get("markPrice", 0))
                    fr = m.get("fundingRate", {})
                    if isinstance(fr, dict):
                        funding_pct = float(fr.get("long", fr.get("short", 0)))
                    else:
                        funding_pct = float(fr or 0)
                    break
        except Exception as e:
            print(f"  Warning: failed to fetch markets: {e}")

        # Orderbook from DLOB
        best_bid = 0.0
        best_ask = 0.0
        try:
            resp = await client.get(f"{DLOB_API}/l2", params={
                "marketName": symbol,
                "marketType": "perp",
                "depth": 5,
            })
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

        ann_rate = funding_pct * 8760 / 100  # pct to decimal, then annualize
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else oracle_price
        spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 and best_bid and best_ask else 0
        dist_pct = (price - mid) / mid * 100 if mid > 0 else 0

        print("=" * 60)
        print(f"  DRIFT LIMIT ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:       {symbol} (marketIndex={market_index})")
        print(f"  Side:         {side_label}")
        print(f"  Size:         {size}")
        print(f"  Price:        ${price:,.4f}")
        print(f"  Notional:     ${notional:,.2f}")
        print(f"  TIF:          {tif}")
        print(f"  Reduce Only:  {reduce_only}")

        print(f"\n  --- Market Context ---")
        print(f"  Oracle Price: ${oracle_price:,.4f}")
        print(f"  Mark Price:   ${mark_price:,.4f}")
        print(f"  Funding:      {funding_pct:.6f}%/hr ({ann_rate*100:.2f}% ann)")
        if best_bid and best_ask:
            print(f"  Best Bid:     ${best_bid:,.4f}")
            print(f"  Best Ask:     ${best_ask:,.4f}")
            print(f"  Spread:       {spread_bps:.2f} bps")
            print(f"  Limit vs Mid: {dist_pct:+.3f}%")

        # ── Account balance ──
        if DEFAULT_ADDRESS:
            account_id = await resolve_account_id(client, DEFAULT_ADDRESS)
            if account_id:
                try:
                    resp = await client.get(f"{DATA_API}/user/{account_id}")
                    resp.raise_for_status()
                    data = resp.json()
                    acct = data.get("account", {})
                    collateral = float(acct.get("totalCollateral", 0))
                    free_coll = float(acct.get("freeCollateral", 0))
                    health = acct.get("health", "?")
                    print(f"\n  --- Account ---")
                    print(f"  NAV:          ${collateral:,.2f}")
                    print(f"  Free Margin:  ${free_coll:,.2f}")
                    print(f"  Health:       {health}%")
                except Exception as e:
                    print(f"\n  --- Account ---")
                    print(f"  Error: {e}")
            else:
                print(f"\n  --- Account ---")
                print(f"  Account not found for address")
        else:
            print(f"\n  --- Account ---")
            print(f"  [DRIFT_WALLET_ADDRESS not set]")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # ── Place order ──
        print(f"\n  Placing order...")

        # Try Drift Gateway first
        gateway_available = False
        try:
            health_resp = await client.get(f"{GATEWAY_URL}/v2/health", timeout=3.0)
            gateway_available = health_resp.status_code == 200
        except Exception:
            pass

        if gateway_available:
            print(f"  Using Drift Gateway at {GATEWAY_URL}")

            # Map TIF for gateway
            tif_gateway_map = {
                "GTC": "gtc",
                "IOC": "ioc",
                "GTX": "postOnly",
            }

            order_body = {
                "marketIndex": market_index,
                "marketType": "perp",
                "amount": size if is_buy else -size,
                "price": price,
                "orderType": "limit",
                "timeInForce": tif_gateway_map.get(tif, "gtc"),
                "reduceOnly": reduce_only,
            }

            try:
                resp = await client.post(f"{GATEWAY_URL}/v2/orders", json=order_body)
                result = resp.json()

                print(f"\n  --- Result ---")
                if resp.status_code == 200:
                    print(f"  Status:       SUBMITTED")
                    print(f"  Order ID:     {result.get('orderId', 'N/A')}")
                    if result.get("tx"):
                        print(f"  Tx:           {result['tx']}")
                else:
                    print(f"  HTTP {resp.status_code}")
                    print(f"  Error:        {result}")
            except Exception as e:
                print(f"\n  --- Result ---")
                print(f"  Error:        {e}")
                return
        else:
            # Fallback to driftpy SDK
            print(f"  Drift Gateway not available. Using driftpy SDK...")

            if not DRIFT_PRIVATE_KEY:
                print("  ERROR: DRIFT_PRIVATE_KEY (or SOLANA_PRIVATE_KEY) must be set in .env")
                print("  Alternatively, run Drift Gateway and set DRIFT_GATEWAY_URL")
                sys.exit(1)

            try:
                from driftpy.drift_client import DriftClient
                from driftpy.constants.config import configs
                from driftpy.types import OrderParams, OrderType, MarketType, PositionDirection, OrderTriggerCondition
                from solders.keypair import Keypair
                from anchorpy import Wallet, Provider
                from solana.rpc.async_api import AsyncClient as SolanaClient
                import base58
            except ImportError:
                print("  ERROR: driftpy not installed. Install with:")
                print("    pip install driftpy")
                print("  Or run Drift Gateway for REST-based order placement.")
                sys.exit(1)

            try:
                # Parse private key (base58 or bytes)
                try:
                    secret = base58.b58decode(DRIFT_PRIVATE_KEY)
                    kp = Keypair.from_bytes(secret)
                except Exception:
                    kp = Keypair.from_base58_string(DRIFT_PRIVATE_KEY)

                wallet = Wallet(kp)
                rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
                connection = SolanaClient(rpc_url)
                provider = Provider(connection, wallet)

                config = configs["mainnet"]
                drift_client = DriftClient(
                    provider.connection,
                    provider.wallet,
                    env="mainnet",
                )
                await drift_client.subscribe()

                # Map TIF
                from driftpy.types import OrderType as DriftOrderType
                if tif == "GTX":
                    order_type = DriftOrderType.LIMIT()
                    post_only = True
                else:
                    order_type = DriftOrderType.LIMIT()
                    post_only = False

                direction = PositionDirection.Long() if is_buy else PositionDirection.Short()

                order_params = OrderParams(
                    order_type=order_type,
                    market_index=market_index,
                    market_type=MarketType.Perp(),
                    direction=direction,
                    base_asset_amount=int(size * 1e9),  # BASE_PRECISION = 1e9
                    price=int(price * 1e6),  # PRICE_PRECISION = 1e6
                    reduce_only=reduce_only,
                    post_only=post_only if tif == "GTX" else None,
                    immediate_or_cancel=True if tif == "IOC" else None,
                    trigger_condition=OrderTriggerCondition.Above(),
                )

                tx_sig = await drift_client.place_perp_order(order_params)

                print(f"\n  --- Result ---")
                print(f"  Status:       SUBMITTED")
                print(f"  Tx Signature: {tx_sig}")

                await drift_client.unsubscribe()
                await connection.close()

            except Exception as e:
                print(f"\n  --- Result ---")
                print(f"  Error:        {e}")
                return

        # ── Updated account ──
        if DEFAULT_ADDRESS:
            try:
                await asyncio.sleep(3)  # Wait for indexer
                account_id = await resolve_account_id(client, DEFAULT_ADDRESS)
                if account_id:
                    resp2 = await client.get(f"{DATA_API}/user/{account_id}")
                    resp2.raise_for_status()
                    data2 = resp2.json()
                    acct2 = data2.get("account", {})
                    new_coll = float(acct2.get("totalCollateral", 0))
                    new_free = float(acct2.get("freeCollateral", 0))
                    print(f"\n  --- Updated Account ---")
                    print(f"  NAV:          ${new_coll:,.2f}")
                    print(f"  Free Margin:  ${new_free:,.2f}")
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
