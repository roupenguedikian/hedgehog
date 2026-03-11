#!/usr/bin/env python3
"""
EdgeX DEX market (taker) order tool — used by the /edgex-market Claude skill.

Places an IOC limit order at an aggressive price to simulate a market order
with slippage protection. Uses ECDSA(SHA3-256) signing.

Usage:
    python3 scripts/edgex_market_order.py <symbol> <side> <size> [--slippage-bps N] [--reduce-only] [--dry-run]

Examples:
    python3 scripts/edgex_market_order.py BTC long 0.001
    python3 scripts/edgex_market_order.py ETH short 0.5 --slippage-bps 30
    python3 scripts/edgex_market_order.py SOL long 10 --reduce-only

Environment variables (.env):
    EDGEX_ACCOUNT_ID     — EdgeX account ID
    EDGEX_PRIVATE_KEY    — secp256k1 private key hex (for signing)
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urlencode

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

BASE = "https://pro.edgex.exchange"
ACCOUNT_ID = os.environ.get("EDGEX_ACCOUNT_ID", "")
PRIVATE_KEY = os.environ.get("EDGEX_PRIVATE_KEY", "")

# ANSI colors
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
X = "\033[0m"

# Symbol cleaning: BTCUSD→BTC, BNB2USD→BNB, 1000PEPE2USD→1000PEPE
_SYM_RE = re.compile(r"^(1000(?:PEPE|SATS|SHIB|BONK|FLOKI)|[A-Z0-9]+?)2?USD$")


def _clean_symbol(contract_name: str) -> str:
    m = _SYM_RE.match(contract_name)
    return m.group(1) if m else contract_name.replace("USD", "")


# ═══════════════════════════════════════════════════════════════
# AUTH — ECDSA(SHA3-256) request signing
# ═══════════════════════════════════════════════════════════════

def _sign_request(method: str, path: str, params: dict | None = None, body: str = "") -> dict:
    """Build auth headers for a private API request."""
    from eth_account import Account

    ts = str(int(time.time() * 1000))

    # Build message: timestamp + METHOD + path + sorted_params_or_body
    param_str = ""
    if params:
        param_str = urlencode(sorted(params.items()))
    elif body:
        param_str = body

    message = ts + method.upper() + path + param_str
    msg_hash = hashlib.sha3_256(message.encode()).digest()

    acct = Account.from_key(PRIVATE_KEY)
    sig = acct.unsafe_sign_hash(msg_hash)
    sig_hex = sig.signature.hex()

    return {
        "X-edgeX-Api-Timestamp": ts,
        "X-edgeX-Api-Signature": sig_hex,
    }


async def _private_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    """Authenticated GET request."""
    headers = _sign_request("GET", path, params)
    resp = await client.get(f"{BASE}{path}", params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "SUCCESS":
        raise RuntimeError(f"EdgeX API error: {data.get('msg', data.get('code'))}")
    return data["data"]


async def _private_post(client: httpx.AsyncClient, path: str, body: dict):
    """Authenticated POST request with JSON body."""
    body_str = json.dumps(body, separators=(",", ":"))
    headers = _sign_request("POST", path, body=body_str)
    headers["Content-Type"] = "application/json"
    resp = await client.post(f"{BASE}{path}", content=body_str, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "SUCCESS":
        raise RuntimeError(f"EdgeX API error: {data.get('msg', data.get('code'))}")
    return data["data"]


# ═══════════════════════════════════════════════════════════════
# METADATA — contract ID ↔ symbol mapping
# ═══════════════════════════════════════════════════════════════

async def _get_metadata(client: httpx.AsyncClient) -> dict:
    resp = await client.get(f"{BASE}/api/v1/public/meta/getMetaData")
    resp.raise_for_status()
    meta = resp.json()["data"]

    contracts = {}
    for c in meta["contractList"]:
        name = c["contractName"]
        if name.startswith("TEMP"):
            continue
        contracts[c["contractId"]] = {
            "name": name,
            "symbol": _clean_symbol(name),
            "taker_fee": float(c.get("defaultTakerFeeRate", 0.00038)),
            "maker_fee": float(c.get("defaultMakerFeeRate", 0.00018)),
            "step_size": c.get("stepSize", "0.001"),
            "tick_size": c.get("tickSize", "0.1"),
            "max_leverage": int(c.get("displayMaxLeverage", 100)),
            "funding_interval_min": int(c.get("fundingRateIntervalMin", 240)),
        }
    return contracts


def _find_contract(contracts: dict, symbol: str) -> tuple[str, dict] | None:
    """Find contract ID by cleaned symbol. Prefers higher-traffic (non-v2) contract."""
    matches = []
    for cid, info in contracts.items():
        if info["symbol"].upper() == symbol.upper():
            matches.append((cid, info))
    if not matches:
        return None
    # Prefer contract whose name does NOT contain "2USD" (v1 over v2)
    matches.sort(key=lambda x: ("2USD" in x[1]["name"], x[0]))
    return matches[0]


# ═══════════════════════════════════════════════════════════════
# ARGS
# ═══════════════════════════════════════════════════════════════

def parse_args(args):
    if len(args) < 3:
        print("Usage: python3 scripts/edgex_market_order.py <symbol> <side> <size> [options]")
        print()
        print("  symbol:          BTC, ETH, SOL, etc. (bare symbols)")
        print("  side:            long/buy or short/sell")
        print("  size:            quantity in base asset")
        print()
        print("Options:")
        print("  --slippage-bps N  max slippage in basis points (default: 20)")
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


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    args = sys.argv[1:]
    symbol, is_buy, size, slippage_bps, reduce_only, dry_run = parse_args(args)

    if not ACCOUNT_ID:
        print("ERROR: EDGEX_ACCOUNT_ID not set in .env")
        sys.exit(1)
    if not PRIVATE_KEY:
        print("ERROR: EDGEX_PRIVATE_KEY not set in .env")
        sys.exit(1)

    side_label = "BUY/LONG" if is_buy else "SELL/SHORT"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # ── Metadata: resolve symbol → contract ID ──
        contracts = await _get_metadata(client)
        match = _find_contract(contracts, symbol)
        if not match:
            available = sorted({info["symbol"] for info in contracts.values()})
            print(f"ERROR: Symbol '{symbol}' not found on EdgeX")
            print(f"  Available: {', '.join(available[:25])}...")
            sys.exit(1)

        contract_id, contract_info = match
        contract_name = contract_info["name"]
        tick_size = contract_info["tick_size"]
        step_size = contract_info["step_size"]
        taker_fee = contract_info["taker_fee"]
        interval_min = contract_info["funding_interval_min"]

        # Determine tick/step decimal precision
        tick_decimals = len(tick_size.rstrip("0").split(".")[-1]) if "." in tick_size else 0
        step_decimals = len(step_size.rstrip("0").split(".")[-1]) if "." in step_size else 0

        # Round size to step_size precision
        size = round(size, step_decimals)

        # ── Market context: funding rate ──
        funding_rate = 0.0
        mark_price = 0.0
        index_price = 0.0
        try:
            resp = await client.get(
                f"{BASE}/api/v1/public/funding/getLatestFundingRate",
                params={"contractId": contract_id},
            )
            if resp.status_code == 200 and "<!DOCTYPE" not in resp.text[:50]:
                items = resp.json().get("data", [])
                if items:
                    d = items[0]
                    funding_rate = float(d.get("fundingRate") or 0)
                    mark_price = float(d.get("markPrice") or d.get("oraclePrice") or 0)
                    index_price = float(d.get("indexPrice") or 0)
        except Exception:
            pass

        # ── Market context: ticker for last price ──
        last_price = 0.0
        try:
            resp = await client.get(
                f"{BASE}/api/v1/public/quote/getTicker/",
                params={"contractId": contract_id},
            )
            if resp.status_code == 200 and "<!DOCTYPE" not in resp.text[:50]:
                items = resp.json().get("data", [])
                if items:
                    last_price = float(items[0].get("lastPrice") or 0)
        except Exception:
            pass

        # ── Market context: orderbook depth ──
        best_bid = 0.0
        best_ask = 0.0
        try:
            resp = await client.get(
                f"{BASE}/api/v1/public/quote/getDepth",
                params={"contractId": contract_id},
            )
            if resp.status_code == 200 and "<!DOCTYPE" not in resp.text[:50]:
                depth = resp.json().get("data", {})
                bids = depth.get("bids", depth.get("b", []))
                asks = depth.get("asks", depth.get("a", []))
                if bids:
                    best_bid = float(bids[0][0]) if isinstance(bids[0], list) else float(bids[0].get("price", 0))
                if asks:
                    best_ask = float(asks[0][0]) if isinstance(asks[0], list) else float(asks[0].get("price", 0))
        except Exception:
            pass

        # Fallback: use mark price if orderbook failed
        if best_bid == 0 and mark_price > 0:
            best_bid = mark_price
        if best_ask == 0 and mark_price > 0:
            best_ask = mark_price

        # Calculate aggressive IOC price with slippage
        slippage_mult = slippage_bps / 10000
        if is_buy:
            aggressive_price = best_ask * (1 + slippage_mult)
        else:
            aggressive_price = best_bid * (1 - slippage_mult)

        # Round to tick_size precision
        aggressive_price = round(aggressive_price, tick_decimals)

        ref_price = best_ask if is_buy else best_bid
        notional = size * aggressive_price
        spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 10000 if best_bid and best_ask else 0

        # Annualize funding
        cycle_h = interval_min / 60
        ann_rate = funding_rate * (8760 / cycle_h) * 100

        print("=" * 60)
        print(f"  EDGEX MARKET ORDER {'(DRY RUN)' if dry_run else ''}")
        print("=" * 60)
        print(f"\n  Symbol:        {symbol} ({contract_name})")
        print(f"  Contract ID:   {contract_id}")
        print(f"  Side:          {side_label}")
        print(f"  Size:          {size}")
        print(f"  Slippage:      {slippage_bps} bps")
        print(f"  Limit Price:   ${aggressive_price:,.4f} (IOC with slippage cap)")
        print(f"  Max Notional:  ${notional:,.2f}")
        print(f"  Reduce Only:   {reduce_only}")
        print(f"  Taker Fee:     {taker_fee*100:.2f}%")

        print(f"\n  --- Market Context ---")
        print(f"  Mark Price:    ${mark_price:,.4f}")
        print(f"  Index Price:   ${index_price:,.4f}")
        print(f"  Last Price:    ${last_price:,.4f}")
        print(f"  Best Bid:      ${best_bid:,.4f}")
        print(f"  Best Ask:      ${best_ask:,.4f}")
        print(f"  Spread:        {spread_bps:.2f} bps")
        print(f"  Funding:       {funding_rate*100:.6f}%/{cycle_h:.0f}h ({ann_rate:+.2f}% ann)")

        # ── Account balance ──
        try:
            acct_data = await _private_get(
                client,
                "/api/v1/private/account/getAccountAsset",
                {"accountId": ACCOUNT_ID},
            )
            total_equity = float(acct_data.get("totalEquityValue", 0))
            available = float(acct_data.get("availableAmount", 0))
            print(f"\n  --- Account ---")
            print(f"  NAV:           ${total_equity:,.2f}")
            print(f"  Free Margin:   ${available:,.2f}")
        except Exception as e:
            print(f"\n  --- Account ---")
            print(f"  Error: {e}")

        if dry_run:
            print(f"\n  [DRY RUN] Order NOT placed. Remove --dry-run to execute.")
            return

        # ── Place IOC limit order ──
        print(f"\n  Placing IOC order...")
        order_body = {
            "accountId": ACCOUNT_ID,
            "contractId": contract_id,
            "side": "BUY" if is_buy else "SELL",
            "type": "LIMIT",
            "timeInForce": "IMMEDIATE_OR_CANCEL",
            "size": str(size),
            "price": str(aggressive_price),
        }
        if reduce_only:
            order_body["reduceOnly"] = True

        try:
            result = await _private_post(
                client,
                "/api/v1/private/order/createOrder",
                order_body,
            )

            print(f"\n  --- Result ---")
            order_id = result.get("orderId", "N/A")
            status = result.get("status", "UNKNOWN")
            print(f"  Order ID:      {order_id}")
            print(f"  Status:        {status}")

            # Check for fill info
            filled_size = float(result.get("cumFillSize", result.get("filledSize", 0)))
            avg_price = float(result.get("cumFillValue", 0)) / filled_size if filled_size > 0 else 0
            if result.get("avgFillPrice"):
                avg_price = float(result["avgFillPrice"])

            if filled_size > 0:
                print(f"  Filled:        {filled_size} @ ${avg_price:,.4f}")
                actual_bps = abs(avg_price - ref_price) / ref_price * 10000 if ref_price > 0 else 0
                print(f"  Actual Slip:   {actual_bps:.2f} bps")
            else:
                print(f"  Filled:        0 (order may still be processing)")
                print(f"  Check /edgex fills or orders for status")

        except Exception as e:
            print(f"\n  --- Result ---")
            print(f"  {R}Error: {e}{X}")
            sys.exit(1)

        # ── Updated account ──
        try:
            acct2 = await _private_get(
                client,
                "/api/v1/private/account/getAccountAsset",
                {"accountId": ACCOUNT_ID},
            )
            new_equity = float(acct2.get("totalEquityValue", 0))
            new_avail = float(acct2.get("availableAmount", 0))
            print(f"\n  --- Updated Account ---")
            print(f"  NAV:           ${new_equity:,.2f}")
            print(f"  Free Margin:   ${new_avail:,.2f}")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
