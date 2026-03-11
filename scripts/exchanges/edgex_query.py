#!/usr/bin/env python3
"""
Query EdgeX DEX for funding rates, markets, account info, positions, orders, fills.
Public endpoints need no auth. Private endpoints use ECDSA(SHA3) signing.

Usage:
    python3 connectors/edgex_query.py [funding|markets|account|positions|orders|fills|all]

Environment variables (.env):
    EDGEX_ACCOUNT_ID     — EdgeX account ID (for private endpoints)
    EDGEX_PRIVATE_KEY    — secp256k1 private key hex (for signing)
"""
import asyncio
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://pro.edgex.exchange"
DEFAULT_ACCOUNT_ID = os.getenv("EDGEX_ACCOUNT_ID", "")
PRIVATE_KEY = os.getenv("EDGEX_PRIVATE_KEY", "")

# ANSI colors
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
D = "\033[2m"
X = "\033[0m"

# Symbol cleaning: BTCUSD→BTC, BNB2USD→BNB, 1000PEPE2USD→1000PEPE
_SYM_RE = re.compile(r"^(1000(?:PEPE|SATS|SHIB|BONK|FLOKI)|[A-Z0-9]+?)2?USD$")


def _clean_symbol(contract_name: str) -> str:
    m = _SYM_RE.match(contract_name)
    return m.group(1) if m else contract_name.replace("USD", "")


# ═══════════════════════════════════════════════════════════════
# AUTH — ECDSA(SHA3-256) request signing
# ═══════════════════════════════════════════════════════════════

def _sign_request(method: str, path: str, params: dict | None = None) -> dict:
    """Build auth headers for a private API request."""
    if not PRIVATE_KEY:
        raise RuntimeError("EDGEX_PRIVATE_KEY not set in .env")

    from eth_account import Account

    ts = str(int(time.time() * 1000))

    # Build message: timestamp + METHOD + path + sorted_params
    param_str = ""
    if params:
        param_str = urlencode(sorted(params.items()))

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


# ═══════════════════════════════════════════════════════════════
# METADATA CACHE — contract ID ↔ symbol mapping
# ═══════════════════════════════════════════════════════════════

_meta_cache: dict | None = None


async def _get_metadata(client: httpx.AsyncClient) -> dict:
    global _meta_cache
    if _meta_cache:
        return _meta_cache

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

    coins = {}
    for c in meta.get("coinList", []):
        coins[c["coinId"]] = c.get("coinName", c["coinId"])

    _meta_cache = {"contracts": contracts, "coins": coins}
    return _meta_cache


def _contract_sym(meta: dict, contract_id: str) -> str:
    c = meta["contracts"].get(contract_id)
    return c["symbol"] if c else f"CID_{contract_id}"


def _contract_name(meta: dict, contract_id: str) -> str:
    c = meta["contracts"].get(contract_id)
    return c["name"] if c else f"CID_{contract_id}"


# ═══════════════════════════════════════════════════════════════
# BATCHED TICKER FETCH — avoid Cloudflare rate limits
# ═══════════════════════════════════════════════════════════════

BATCH_SIZE = 15
BATCH_DELAY = 0.3  # seconds between batches


async def _fetch_all_tickers(client: httpx.AsyncClient, contract_ids: list, build_fn):
    """Fetch tickers in batches to avoid 403 rate limits."""
    all_results = []
    for i in range(0, len(contract_ids), BATCH_SIZE):
        batch = contract_ids[i : i + BATCH_SIZE]
        results = await asyncio.gather(*[build_fn(cid) for cid in batch])
        all_results.extend(results)
        if i + BATCH_SIZE < len(contract_ids):
            await asyncio.sleep(BATCH_DELAY)
    return all_results


# ═══════════════════════════════════════════════════════════════
# PUBLIC — no auth needed
# ═══════════════════════════════════════════════════════════════

async def query_funding(client: httpx.AsyncClient):
    """Funding rates for all markets + extreme funding scanner."""
    meta = await _get_metadata(client)
    contracts = meta["contracts"]

    print("=" * 82)
    print("  EdgeX — FUNDING RATES")
    print("=" * 82)

    # Step 1: Fetch funding rates via dedicated endpoint (more reliable)
    async def _get_funding(cid):
        try:
            r = await client.get(
                f"{BASE}/api/v1/public/funding/getLatestFundingRate",
                params={"contractId": cid},
            )
            if r.status_code != 200 or "<!DOCTYPE" in r.text[:50]:
                return None
            items = r.json().get("data", [])
            if not items:
                return None
            d = items[0]
            return cid, {
                "rate": float(d.get("fundingRate") or 0),
                "mark": float(d.get("markPrice") or d.get("oraclePrice") or 0),
                "index": float(d.get("indexPrice") or 0),
            }
        except Exception:
            return None

    # Step 2: Fetch tickers for volume/price data (best-effort, may be Cloudflare-blocked)
    async def _get_ticker(cid):
        try:
            r = await client.get(
                f"{BASE}/api/v1/public/quote/getTicker/",
                params={"contractId": cid},
            )
            if r.status_code != 200 or "<!DOCTYPE" in r.text[:50]:
                return None
            items = r.json().get("data", [])
            if not items:
                return None
            d = items[0]
            return cid, {
                "last": float(d.get("lastPrice") or 0),
                "oi": float(d.get("openInterest") or 0),
                "volume": float(d.get("value") or 0),
                "high": float(d.get("high") or 0),
                "low": float(d.get("low") or 0),
                "change_pct": float(d.get("priceChangePercent") or 0),
            }
        except Exception:
            return None

    cids = list(contracts.keys())

    # Batch funding fetches
    funding_data: dict[str, dict] = {}
    for i in range(0, len(cids), BATCH_SIZE):
        batch = cids[i : i + BATCH_SIZE]
        results = await asyncio.gather(*[_get_funding(cid) for cid in batch])
        for r in results:
            if r:
                funding_data[r[0]] = r[1]
        if i + BATCH_SIZE < len(cids):
            await asyncio.sleep(BATCH_DELAY)

    # Batch ticker fetches (best-effort)
    ticker_data: dict[str, dict] = {}
    ticker_blocked = False
    for i in range(0, len(cids), BATCH_SIZE):
        if ticker_blocked:
            break
        batch = cids[i : i + BATCH_SIZE]
        results = await asyncio.gather(*[_get_ticker(cid) for cid in batch])
        got_any = False
        for r in results:
            if r:
                ticker_data[r[0]] = r[1]
                got_any = True
        if not got_any and i == 0:
            ticker_blocked = True
        if i + BATCH_SIZE < len(cids):
            await asyncio.sleep(BATCH_DELAY)

    if ticker_blocked:
        print(f"  {Y}Note: Ticker endpoint blocked by Cloudflare — volume/OI unavailable{X}")

    # Merge funding + ticker data into asset list
    raw = []
    for cid, fd in funding_data.items():
        if cid not in contracts:
            continue
        td = ticker_data.get(cid, {})
        raw.append({
            "contract_id": cid,
            "symbol": contracts[cid]["symbol"],
            "contract_name": contracts[cid]["name"],
            "rate": fd["rate"],
            "last": td.get("last", 0),
            "mark": fd["mark"],
            "index": fd["index"],
            "oi": td.get("oi", 0),
            "volume": td.get("volume", 0),
            "high": td.get("high", 0),
            "low": td.get("low", 0),
            "change_pct": td.get("change_pct", 0),
            "interval_min": contracts[cid]["funding_interval_min"],
        })

    # Deduplicate: keep higher-volume entry when symbol appears twice (v1 + v2 contracts)
    seen: dict[str, dict] = {}
    for a in raw:
        sym = a["symbol"]
        if sym not in seen or a["volume"] > seen[sym]["volume"]:
            seen[sym] = a
    assets = list(seen.values())

    # Compute annualized rates
    for a in assets:
        cycle_h = a["interval_min"] / 60
        a["annualized"] = a["rate"] * (8760 / cycle_h) * 100
        a["oi_usd"] = a["oi"] * a["mark"]

    # Sort by 24h volume
    by_vol = sorted(assets, key=lambda a: a["volume"], reverse=True)

    print(f"\n  {'SYMBOL':<12} {'RATE/4H':>12} {'ANNUAL':>8} {'MARK':>12} "
          f"{'24H CHG':>8} {'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 86)
    for a in by_vol[:25]:
        ann = a["annualized"]
        color = G if ann > 0 else R if ann < 0 else ""
        reset = X if color else ""
        print(f"  {a['symbol']:<12} {a['rate']*100:>11.6f}% "
              f"{color}{ann:>+7.2f}%{reset} "
              f"${a['mark']:>11,.2f} {a['change_pct']*100:>+7.2f}% "
              f"${a['oi_usd']:>13,.0f} ${a['volume']:>13,.0f}")

    # Extreme funding
    by_rate = sorted(assets, key=lambda a: a["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':<12} {'ANNUAL':>8} {'MARK':>12} {'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 66)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:<12} {G}{a['annualized']:>+7.2f}%{X} "
              f"${a['mark']:>11,.4f} ${a['oi_usd']:>13,.0f} ${a['volume']:>13,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:<12} {R}{a['annualized']:>+7.2f}%{X} "
              f"${a['mark']:>11,.4f} ${a['oi_usd']:>13,.0f} ${a['volume']:>13,.0f}")

    total_vol = sum(a["volume"] for a in assets)
    total_oi = sum(a["oi_usd"] for a in assets)
    avg_rate = sum(a["annualized"] for a in by_vol[:20]) / min(20, len(by_vol)) if by_vol else 0
    print(f"\n  Funding cycle: 4h (2190 payments/year)")
    print(f"  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Total open interest: ${total_oi:,.0f}")
    print(f"  Active markets: {len(assets)}")
    print(f"  Avg annualized rate (top 20): {avg_rate:+.2f}%")


async def query_markets(client: httpx.AsyncClient):
    """All active markets with price, volume, OI."""
    meta = await _get_metadata(client)
    contracts = meta["contracts"]

    print("=" * 82)
    print("  EdgeX — ALL MARKETS")
    print("=" * 82)

    async def _get_ticker(cid):
        try:
            r = await client.get(
                f"{BASE}/api/v1/public/quote/getTicker/",
                params={"contractId": cid},
            )
            if r.status_code != 200 or "<!DOCTYPE" in r.text[:50]:
                return None
            items = r.json().get("data", [])
            if not items:
                return None
            d = items[0]
            return {
                "symbol": contracts[cid]["symbol"],
                "last": float(d.get("lastPrice") or 0),
                "high": float(d.get("high") or 0),
                "low": float(d.get("low") or 0),
                "change_pct": float(d.get("priceChangePercent") or 0),
                "volume": float(d.get("value") or 0),
                "oi": float(d.get("openInterest") or 0),
                "mark": float(d.get("markPrice") or 0),
                "trades": int(d.get("trades") or 0),
            }
        except Exception:
            return None

    results = await _fetch_all_tickers(client, list(contracts.keys()), _get_ticker)
    raw = [r for r in results if r is not None]
    if not raw:
        print(f"\n  {Y}Ticker endpoint blocked by Cloudflare — market data unavailable{X}")
        print(f"  Try again later or use 'funding' subcommand for rates.")
        return

    # Deduplicate by symbol (keep higher volume)
    seen: dict[str, dict] = {}
    for a in raw:
        sym = a["symbol"]
        if sym not in seen or a["volume"] > seen[sym]["volume"]:
            seen[sym] = a
    assets = list(seen.values())
    assets.sort(key=lambda a: a["volume"], reverse=True)

    print(f"\n  {'SYMBOL':<12} {'LAST':>12} {'24H HIGH':>12} {'24H LOW':>12} "
          f"{'CHG%':>8} {'TRADES':>8} {'24H VOL':>14}")
    print("  " + "-" * 84)
    for a in assets:
        if a["volume"] < 1000:
            continue
        chg_c = G if a["change_pct"] > 0 else R if a["change_pct"] < 0 else ""
        print(f"  {a['symbol']:<12} ${a['last']:>11,.2f} ${a['high']:>11,.2f} ${a['low']:>11,.2f} "
              f"{chg_c}{a['change_pct']*100:>+7.2f}%{X} {a['trades']:>8,} ${a['volume']:>13,.0f}")

    print(f"\n  Total markets: {len(assets)} active")


# ═══════════════════════════════════════════════════════════════
# PRIVATE — auth required
# ═══════════════════════════════════════════════════════════════

async def query_account(client: httpx.AsyncClient, account_id: str):
    """Account balance, equity, available margin."""
    print("=" * 82)
    print(f"  EdgeX ACCOUNT — {account_id}")
    print("=" * 82)

    try:
        data = await _private_get(
            client,
            "/api/v1/private/account/getAccountAsset",
            {"accountId": account_id},
        )

        acct = data.get("account", {})
        print(f"\n  Account ID:    {acct.get('id', account_id)}")
        print(f"  ETH Address:   {acct.get('ethAddress', 'N/A')}")
        print(f"  Status:        {acct.get('status', 'N/A')}")

        total_equity = float(data.get("totalEquityValue", 0))
        available = float(data.get("availableAmount", 0))
        margin_used = total_equity - available
        margin_util = (margin_used / total_equity * 100) if total_equity > 0 else 0

        print(f"\n  Total Equity:  ${total_equity:,.2f}")
        print(f"  Available:     ${available:,.2f}")
        print(f"  Margin Used:   ${margin_used:,.2f} ({margin_util:.1f}%)")

        # Collateral breakdown
        collateral_list = data.get("collateralList", data.get("collateralAssetModelList", []))
        if collateral_list:
            print(f"\n  Collateral:")
            for c in collateral_list:
                coin_id = c.get("coinId", "?")
                amount = float(c.get("amount", c.get("totalAmount", 0)))
                if abs(amount) > 0.001:
                    print(f"    {coin_id}: {amount:,.4f}")

        # Position summary
        pos_list = data.get("positionAssetList", [])
        if pos_list:
            print(f"\n  Positions: {len(pos_list)} open")
        else:
            print(f"\n  No open positions")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_positions(client: httpx.AsyncClient, account_id: str):
    """Open positions with size, value, PnL."""
    meta = await _get_metadata(client)

    print("=" * 82)
    print(f"  EdgeX POSITIONS — {account_id}")
    print("=" * 82)

    try:
        data = await _private_get(
            client,
            "/api/v1/private/account/getAccountAsset",
            {"accountId": account_id},
        )

        positions = data.get("positionAssetList", [])
        if not positions:
            print("\n  No open positions")
            return

        print(f"\n  {'SYMBOL':<12} {'SIDE':<6} {'SIZE':>12} {'VALUE':>14} "
              f"{'ENTRY':>12} {'MARK':>12} {'uPnL':>12}")
        print("  " + "-" * 86)

        total_upnl = 0.0
        for p in positions:
            cid = p.get("contractId", "")
            sym = _contract_sym(meta, cid)
            size = float(p.get("openSize", 0))
            if abs(size) < 1e-12:
                continue
            value = float(p.get("openValue", 0))
            upnl = float(p.get("unrealizePnl", 0))
            total_upnl += upnl

            entry = abs(value / size) if size != 0 else 0
            mark = float(p.get("markPrice", entry))
            side = "LONG" if size > 0 else "SHORT"

            upnl_c = G if upnl > 0 else R if upnl < 0 else ""
            print(f"  {sym:<12} {side:<6} {abs(size):>12.4f} ${abs(value):>13,.2f} "
                  f"${entry:>11,.2f} ${mark:>11,.2f} {upnl_c}${upnl:>+11,.2f}{X}")

        print(f"\n  Total uPnL: ${total_upnl:+,.2f}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_orders(client: httpx.AsyncClient, account_id: str):
    """Active open orders."""
    meta = await _get_metadata(client)

    print("=" * 82)
    print(f"  EdgeX OPEN ORDERS — {account_id}")
    print("=" * 82)

    try:
        data = await _private_get(
            client,
            "/api/v1/private/order/getActiveOrderPage",
            {"accountId": account_id, "size": "50"},
        )

        orders = data.get("dataList", [])
        if not orders:
            print("\n  No open orders")
            return

        print(f"\n  {'SYMBOL':<12} {'SIDE':<6} {'SIZE':>10} {'PRICE':>14} "
              f"{'FILLED':>10} {'TYPE':<10} {'STATUS':<12}")
        print("  " + "-" * 80)
        for o in orders:
            cid = o.get("contractId", "")
            sym = _contract_sym(meta, cid)
            size = o.get("size", "0")
            price = o.get("price", "0")
            filled = o.get("cumFillSize", o.get("filledSize", "0"))
            otype = o.get("type", "?")
            side = o.get("side", "?")
            status = o.get("status", "?")
            print(f"  {sym:<12} {side:<6} {size:>10} ${float(price):>13,.2f} "
                  f"{filled:>10} {otype:<10} {status:<12}")

        print(f"\n  Total open orders: {len(orders)}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_fills(client: httpx.AsyncClient, account_id: str):
    """Recent trade fills."""
    meta = await _get_metadata(client)

    print("=" * 82)
    print(f"  EdgeX RECENT FILLS — {account_id}")
    print("=" * 82)

    try:
        data = await _private_get(
            client,
            "/api/v1/private/order/getHistoryOrderFillTransactionPage",
            {"accountId": account_id, "size": "30"},
        )

        fills = data.get("dataList", [])
        if not fills:
            print("\n  No recent fills")
            return

        print(f"\n  {'TIME':<20} {'SYMBOL':<10} {'SIDE':<6} {'SIZE':>10} "
              f"{'PRICE':>14} {'VALUE':>14} {'PnL':>12} {'ROLE':<6}")
        print("  " + "-" * 98)
        for f in fills:
            cid = f.get("contractId", "")
            sym = _contract_sym(meta, cid)
            size = float(f.get("fillSize", 0))
            price = float(f.get("fillPrice", 0))
            value = float(f.get("fillValue", size * price))
            fee = float(f.get("fillFee", 0))
            pnl = float(f.get("realizePnl", 0))
            role = f.get("direction", "?")
            side = f.get("side", "?")

            # Parse timestamp
            match_time = f.get("matchTime", f.get("createdTime", ""))
            if match_time and match_time.isdigit():
                ts = datetime.fromtimestamp(int(match_time) / 1000, tz=timezone.utc)
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts_str = str(match_time)[:19]

            pnl_c = G if pnl > 0 else R if pnl < 0 else D
            print(f"  {ts_str:<20} {sym:<10} {side:<6} {size:>10.4f} "
                  f"${price:>13,.2f} ${value:>13,.2f} "
                  f"{pnl_c}${pnl:>+11,.2f}{X} {role:<6}")

        print(f"\n  Total fills shown: {len(fills)}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


async def query_funding_payments(client: httpx.AsyncClient, account_id: str):
    """Funding fee settlement history."""
    meta = await _get_metadata(client)

    print("=" * 82)
    print(f"  EdgeX FUNDING PAYMENTS — {account_id}")
    print("=" * 82)

    try:
        data = await _private_get(
            client,
            "/api/v1/private/account/getPositionTransactionPage",
            {
                "accountId": account_id,
                "size": "100",
                "filterTypeList": "SETTLE_FUNDING_FEE",
            },
        )

        txns = data.get("dataList", [])
        if not txns:
            print("\n  No funding payments found")
            return

        # Aggregate by contract
        by_contract: dict[str, list] = {}
        for t in txns:
            cid = t.get("contractId", "?")
            by_contract.setdefault(cid, []).append(t)

        print(f"\n  {'SYMBOL':<12} {'PAYMENTS':>8} {'TOTAL':>12} {'LATEST':>20}")
        print("  " + "-" * 56)

        grand_total = 0.0
        for cid, payments in sorted(by_contract.items()):
            sym = _contract_sym(meta, cid)
            total = sum(float(p.get("realizePnl", 0)) for p in payments)
            grand_total += total

            latest_time = max(
                (p.get("createdTime", "0") for p in payments), default="0"
            )
            if latest_time.isdigit():
                latest_dt = datetime.fromtimestamp(int(latest_time) / 1000, tz=timezone.utc)
                latest_str = latest_dt.strftime("%Y-%m-%d %H:%M")
            else:
                latest_str = str(latest_time)[:16]

            color = G if total > 0 else R if total < 0 else ""
            print(f"  {sym:<12} {len(payments):>8} {color}${total:>+11,.4f}{X} {latest_str:>20}")

        print(f"\n  Grand total: ${grand_total:+,.4f} across {len(txns)} payments")

        # Last 10 individual payments
        print(f"\n  Last 10 payments:")
        print(f"  {'TIME':<20} {'SYMBOL':<10} {'AMOUNT':>12}")
        print("  " + "-" * 44)
        for t in txns[:10]:
            cid = t.get("contractId", "?")
            sym = _contract_sym(meta, cid)
            pnl = float(t.get("realizePnl", 0))
            ts = t.get("createdTime", "0")
            if ts.isdigit():
                dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = str(ts)[:16]
            color = G if pnl > 0 else R if pnl < 0 else ""
            print(f"  {ts_str:<20} {sym:<10} {color}${pnl:>+11,.4f}{X}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {
    "funding": query_funding,
    "markets": query_markets,
}
ACCT_SECTIONS = {
    "account": query_account,
    "positions": query_positions,
    "orders": query_orders,
    "fills": query_fills,
    "income": query_funding_payments,
}
ALL_SECTIONS = {**PUBLIC_SECTIONS, **ACCT_SECTIONS}
ALL_NAMES = list(ALL_SECTIONS.keys()) + ["all"]


async def main():
    args = sys.argv[1:]

    command = "all"
    account_id = DEFAULT_ACCOUNT_ID

    for arg in args:
        if arg.isdigit() and len(arg) > 10:
            account_id = arg
        elif arg.lower() in ALL_NAMES:
            command = arg.lower()

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    async with httpx.AsyncClient(timeout=30.0) as client:
        if command == "all":
            print(f"\n  EdgeX Full Query — {ts_str}\n")
            await query_funding(client)
            print()
            if account_id and PRIVATE_KEY:
                await query_account(client, account_id)
                print()
                await query_positions(client, account_id)
                print()
                await query_orders(client, account_id)
                print()
                await query_fills(client, account_id)
                print()
                await query_funding_payments(client, account_id)
            elif not account_id:
                print(f"  {Y}Skipping private endpoints — EDGEX_ACCOUNT_ID not set{X}")
            else:
                print(f"  {Y}Skipping private endpoints — EDGEX_PRIVATE_KEY not set{X}")
        elif command in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[command](client)
        elif command in ACCT_SECTIONS:
            if not account_id:
                print("Error: EDGEX_ACCOUNT_ID not set in .env")
                sys.exit(1)
            if not PRIVATE_KEY:
                print("Error: EDGEX_PRIVATE_KEY not set in .env")
                sys.exit(1)
            await ACCT_SECTIONS[command](client, account_id)
        else:
            print(f"Unknown command: {command}")
            print(f"Valid: {', '.join(ALL_NAMES)}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
