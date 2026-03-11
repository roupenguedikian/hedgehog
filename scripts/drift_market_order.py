#!/usr/bin/env python3
"""
Drift Protocol market (taker) order tool — used by the /drift-market Claude skill.

Uses Drift Gateway (preferred) or driftpy SDK (fallback).

Usage:
    python3 scripts/drift_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/drift_market_order.py BTC long 0.001
    python3 scripts/drift_market_order.py SOL short 10 --slippage-bps 20
"""
import asyncio
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

load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))

DATA_API = "https://data.api.drift.trade"
DLOB_API = "https://dlob.drift.trade"
GATEWAY_URL = os.getenv("DRIFT_GATEWAY_URL", "http://localhost:8080")
DEFAULT_ADDRESS = os.getenv("DRIFT_WALLET_ADDRESS", "")
DRIFT_PRIVATE_KEY = os.getenv("DRIFT_PRIVATE_KEY", os.getenv("SOLANA_PRIVATE_KEY", ""))

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
    return accounts[0]["accountId"] if accounts else ""


def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/drift_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]")
        print()
        print("  symbol:          BTC, ETH, SOL, etc. (auto-appended -PERP)")
        print("  side:            long/buy or short/sell")
        print("  size:            quantity in base asset")
        print("  --slippage-bps N max slippage in bps (default: 20)")
        print("  --reduce-only    only reduce existing position")
        print("  --dry-run        show what would happen, don't execute")
        sys.exit(1)

    symbol = args[0].upper()
    if not symbol.endswith("-PERP"):
        symbol = symbol + "-PERP"
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

    if symbol not in MARKET_INDICES:
        print(f"ERROR: Symbol '{symbol}' not found. Available: {', '.join(sorted(MARKET_INDICES.keys()))}")
        sys.exit(1)

    market_index = MARKET_INDICES[symbol]
    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    async with httpx.AsyncClient(timeout=20.0) as client:
        # Market context
        oracle_price = 0.0
        funding_pct = 0.0
        best_bid = 0.0
        best_ask = 0.0

        try:
            resp = await client.get(f"{DATA_API}/stats/markets")
            resp.raise_for_status()
            for m in resp.json().get("markets", []):
                if m.get("symbol") == symbol:
                    oracle_price = float(m.get("oraclePrice", 0))
                    fr = m.get("fundingRate", {})
                    funding_pct = float(fr.get("long", 0)) if isinstance(fr, dict) else float(fr or 0)
                    break
        except Exception:
            pass

        try:
            resp = await client.get(f"{DLOB_API}/l2", params={
                "marketName": symbol, "marketType": "perp", "depth": 5,
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
        print(f"  DRIFT MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
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
        print(f"  Funding:       {funding_pct:.6f}%/hr ({funding_pct*8760/100*100:.2f}% ann)")

        # Account
        if DEFAULT_ADDRESS:
            account_id = await resolve_account_id(client, DEFAULT_ADDRESS)
            if account_id:
                try:
                    resp = await client.get(f"{DATA_API}/user/{account_id}")
                    resp.raise_for_status()
                    acct = resp.json().get("account", {})
                    print(f"\n  --- Account ---")
                    print(f"  NAV:           ${float(acct.get('totalCollateral', 0)):,.2f}")
                    print(f"  Free Margin:   ${float(acct.get('freeCollateral', 0)):,.2f}")
                    print(f"  Health:        {acct.get('health', '?')}%")
                except Exception as e:
                    print(f"\n  --- Account ---")
                    print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # Place order
        print(f"\n  Placing market order...")

        # Try Drift Gateway
        gateway_available = False
        try:
            health_resp = await client.get(f"{GATEWAY_URL}/v2/health", timeout=3.0)
            gateway_available = health_resp.status_code == 200
        except Exception:
            pass

        if gateway_available:
            print(f"  Using Drift Gateway at {GATEWAY_URL}")
            order_body = {
                "marketIndex": market_index,
                "marketType": "perp",
                "amount": size if is_buy else -size,
                "price": aggressive_price,
                "orderType": "market",
                "reduceOnly": reduce_only,
            }

            try:
                resp = await client.post(f"{GATEWAY_URL}/v2/orders", json=order_body)
                result = resp.json()
                print(f"\n  --- Result ---")
                if resp.status_code == 200:
                    print(f"  Status:        SUBMITTED")
                    print(f"  Order ID:      {result.get('orderId', 'N/A')}")
                    if result.get("tx"):
                        print(f"  Tx:            {result['tx']}")
                else:
                    print(f"  HTTP {resp.status_code}")
                    print(f"  Error:         {result}")
            except Exception as e:
                print(f"\n  --- Result ---")
                print(f"  Error:         {e}")
                return
        else:
            # Fallback to driftpy
            if not DRIFT_PRIVATE_KEY:
                print("  ERROR: Drift Gateway not available and DRIFT_PRIVATE_KEY not set")
                print("  Either run Drift Gateway or set DRIFT_PRIVATE_KEY in .env")
                sys.exit(1)

            try:
                from driftpy.drift_client import DriftClient
                from driftpy.types import OrderParams, OrderType, MarketType, PositionDirection, OrderTriggerCondition
                from solders.keypair import Keypair
                from anchorpy import Wallet, Provider
                from solana.rpc.async_api import AsyncClient as SolanaClient
                import base58
            except ImportError:
                print("  ERROR: driftpy not installed. Install with: pip install driftpy")
                sys.exit(1)

            try:
                try:
                    secret = base58.b58decode(DRIFT_PRIVATE_KEY)
                    kp = Keypair.from_bytes(secret)
                except Exception:
                    kp = Keypair.from_base58_string(DRIFT_PRIVATE_KEY)

                wallet = Wallet(kp)
                rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
                connection = SolanaClient(rpc_url)
                provider = Provider(connection, wallet)

                drift_client = DriftClient(provider.connection, provider.wallet, env="mainnet")
                await drift_client.subscribe()

                direction = PositionDirection.Long() if is_buy else PositionDirection.Short()

                order_params = OrderParams(
                    order_type=OrderType.MARKET(),
                    market_index=market_index,
                    market_type=MarketType.Perp(),
                    direction=direction,
                    base_asset_amount=int(size * 1e9),
                    price=int(aggressive_price * 1e6),
                    reduce_only=reduce_only,
                    immediate_or_cancel=True,
                    trigger_condition=OrderTriggerCondition.Above(),
                )

                tx_sig = await drift_client.place_perp_order(order_params)

                print(f"\n  --- Result ---")
                print(f"  Status:        SUBMITTED")
                print(f"  Tx Signature:  {tx_sig}")

                await drift_client.unsubscribe()
                await connection.close()

            except Exception as e:
                print(f"\n  --- Result ---")
                print(f"  Error:         {e}")
                return

        # Updated account
        if DEFAULT_ADDRESS:
            try:
                await asyncio.sleep(3)
                account_id = await resolve_account_id(client, DEFAULT_ADDRESS)
                if account_id:
                    resp2 = await client.get(f"{DATA_API}/user/{account_id}")
                    resp2.raise_for_status()
                    acct2 = resp2.json().get("account", {})
                    print(f"\n  --- Updated Account ---")
                    print(f"  NAV:           ${float(acct2.get('totalCollateral', 0)):,.2f}")
                    print(f"  Free Margin:   ${float(acct2.get('freeCollateral', 0)):,.2f}")
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
