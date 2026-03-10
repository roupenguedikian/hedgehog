#!/usr/bin/env python3
"""
Query Apex Omni exchange — public market data + authenticated account data.
"""
import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://omni.apex.exchange"

API_KEY = os.getenv("APEX_OMNI_API_KEY", "")
API_SECRET = os.getenv("APEX_OMNI_API_SECRET", "")
PASSPHRASE = os.getenv("APEX_OMNI_PASSPHRASE", "")


def sign_request(secret: str, timestamp: str, method: str, request_path: str, data_string: str = "") -> str:
    """HMAC-SHA256 signature matching the apexomni SDK."""
    message = timestamp + method.upper() + request_path + data_string
    # SDK uses: base64.standard_b64encode(secret.encode())
    hmac_key = base64.standard_b64encode(secret.encode("utf-8"))
    sig = hmac.new(hmac_key, message.encode("utf-8"), hashlib.sha256)
    return base64.standard_b64encode(sig.digest()).decode()


def auth_headers(method: str, request_path: str, data_string: str = "") -> dict:
    timestamp = str(int(round(time.time() * 1000)))
    sig = sign_request(API_SECRET, timestamp, method, request_path, data_string)
    return {
        "APEX-SIGNATURE": sig,
        "APEX-API-KEY": API_KEY,
        "APEX-TIMESTAMP": timestamp,
        "APEX-PASSPHRASE": PASSPHRASE,
    }


def auth_get(client: httpx.Client, path: str, params: dict | None = None) -> httpx.Response:
    """Authenticated GET — query params are included in the signed path."""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
        full_path = f"{path}?{qs}" if qs else path
    else:
        full_path = path
    headers = auth_headers("GET", full_path)
    return client.get(f"{BASE}{path}", headers=headers, params=params)


def main():
    with httpx.Client(timeout=15.0) as c:
        print("=" * 70)
        print("  APEX OMNI EXCHANGE QUERY")
        print("=" * 70)

        # ── 1. Funding Rates ────────────────────────────────────────────
        print("\n  FUNDING RATES")
        print("-" * 70)
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT",
                    "LINKUSDT", "ARBUSDT", "AVAXUSDT", "WIFUSDT", "NEARUSDT", "AAVEUSDT"]
        for sym in symbols:
            try:
                r = c.get(f"{BASE}/api/v3/ticker", params={"symbol": sym})
                items = r.json().get("data", [])
                if items and isinstance(items, list) and items[0]:
                    d = items[0]
                    fr = d.get("fundingRate", "?")
                    pfr = d.get("predictedFundingRate", "?")
                    last = d.get("lastPrice", "?")
                    oi = d.get("openInterest", "?")
                    hi = d.get("highPrice24h", "?")
                    lo = d.get("lowPrice24h", "?")
                    try:
                        ann = float(fr) * (8760 / 8) * 100
                        ann_s = f"{ann:+.2f}%"
                    except (ValueError, TypeError):
                        ann_s = "?"
                    print(f"  {sym:<12} FR: {fr:>14}  Ann: {ann_s:>8}  "
                          f"Last: {last:>12}  OI: {oi:>12}")
                else:
                    print(f"  {sym:<12} no data")
            except Exception as e:
                print(f"  {sym:<12} error: {e}")

        # ── 2. Orderbook BTC ────────────────────────────────────────────
        print(f"\n  ORDERBOOK — BTCUSDT (top 5)")
        print("-" * 70)
        try:
            r = c.get(f"{BASE}/api/v3/depth", params={"symbol": "BTCUSDT", "limit": 5})
            data = r.json().get("data", {})
            asks = data.get("a", [])[:5]
            bids = data.get("b", [])[:5]
            print(f"  {'ASKS':^33}  |  {'BIDS':^33}")
            print(f"  {'Price':>15} {'Size':>15}  |  {'Price':>15} {'Size':>15}")
            for i in range(5):
                a = f"  {asks[i][0]:>15} {asks[i][1]:>15}" if i < len(asks) else " " * 33
                b = f"  {bids[i][0]:>15} {bids[i][1]:>15}" if i < len(bids) else ""
                print(f"{a}  |{b}")
        except Exception as e:
            print(f"  Error: {e}")

        # ── 3. Authenticated endpoints ──────────────────────────────────
        if not API_KEY:
            print("\n  No APEX_OMNI_API_KEY — skipping auth endpoints")
            return

        print("\n  AUTHENTICATED DATA")
        print("=" * 70)

        # Account
        print("\n  ACCOUNT")
        print("-" * 70)
        r = auth_get(c, "/api/v3/account")
        j = r.json()
        if j.get("data"):
            data = j["data"]
            for key in ["id", "l2Key", "ethereumAddress", "takerFeeRate", "makerFeeRate"]:
                if key in data:
                    print(f"  {key}: {data[key]}")
            # Nested account data
            for sub_key in ["account", "accounts", "wallets"]:
                if sub_key in data and data[sub_key]:
                    acct = data[sub_key]
                    if isinstance(acct, dict):
                        print(f"\n  Account Details:")
                        for k in ["equity", "totalAccountValue", "availableBalance",
                                   "freeCollateral", "totalMarginUsed", "initialMarginRequirement",
                                   "maintenanceMarginRequirement", "unrealizedPnl"]:
                            if k in acct:
                                print(f"    {k}: {acct[k]}")
                        # Open positions
                        positions = acct.get("openPositions", {})
                        if positions:
                            print(f"\n  Open Positions:")
                            for sym, pos in positions.items():
                                side = pos.get("side", "?")
                                size = pos.get("size", "?")
                                entry = pos.get("entryPrice", "?")
                                upnl = pos.get("unrealizedPnl", "?")
                                liq = pos.get("liquidationPrice", "?")
                                print(f"    {sym:<14} {side:<6} Size: {size:>10}  Entry: {entry:>12}  "
                                      f"uPnL: {upnl:>10}  Liq: {liq}")
                    elif isinstance(acct, list):
                        for a in acct:
                            print(f"\n  Account: {a.get('id', '?')}")
                            for k in ["equity", "totalAccountValue", "availableBalance",
                                       "freeCollateral", "totalMarginUsed"]:
                                if k in a:
                                    print(f"    {k}: {a[k]}")
        else:
            print(f"  Response: {j}")

        # Also try v2 account for richer data
        r2 = auth_get(c, "/api/v2/account")
        j2 = r2.json()
        if j2.get("data") and j2.get("code") not in [20016, "20016"]:
            data2 = j2["data"]
            print(f"\n  V2 Account (USDC):")
            for sub in ["account", "wallets"]:
                if sub in data2 and isinstance(data2[sub], dict):
                    for k in ["equity", "totalAccountValue", "availableBalance",
                               "freeCollateral", "totalMarginUsed"]:
                        if k in data2[sub]:
                            print(f"    {k}: {data2[sub][k]}")

        # Account balance
        print("\n  ACCOUNT BALANCE")
        print("-" * 70)
        r = auth_get(c, "/api/v3/account-balance")
        j = r.json()
        if j.get("data"):
            data = j["data"]
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (str, int, float)):
                        print(f"  {k}: {v}")
                    elif isinstance(v, list):
                        print(f"  {k}: ({len(v)} items)")
                        for item in v[:5]:
                            if isinstance(item, dict):
                                print(f"    {item}")
                            else:
                                print(f"    {item}")
            else:
                print(f"  {data}")
        else:
            print(f"  Response: {j}")

        # Open orders
        print("\n  OPEN ORDERS")
        print("-" * 70)
        r = auth_get(c, "/api/v3/open-orders")
        j = r.json()
        if j.get("data"):
            orders = j["data"].get("orders", j["data"]) if isinstance(j["data"], dict) else j["data"]
            if isinstance(orders, list):
                if not orders:
                    print("  No open orders")
                for o in orders:
                    sym = o.get("symbol", "?")
                    side = o.get("side", "?")
                    price = o.get("price", "?")
                    size = o.get("size", "?")
                    otype = o.get("type", "?")
                    print(f"  {sym:<14} {side:<6} {otype:<10} Price: {price:>12}  Size: {size:>10}")
            else:
                print(f"  {orders}")
        else:
            print(f"  Response: {j}")

        # Fills
        print("\n  RECENT FILLS")
        print("-" * 70)
        r = auth_get(c, "/api/v3/fills", {"limit": "10"})
        j = r.json()
        if j.get("data"):
            fills = j["data"].get("fills", j["data"]) if isinstance(j["data"], dict) else j["data"]
            if isinstance(fills, list):
                if not fills:
                    print("  No recent fills")
                for f in fills:
                    sym = f.get("symbol", "?")
                    side = f.get("side", "?")
                    price = f.get("price", "?")
                    size = f.get("size", "?")
                    fee = f.get("fee", "?")
                    ts = f.get("createdAt", "?")
                    print(f"  {sym:<14} {side:<6} Price: {price:>12}  Size: {size:>10}  Fee: {fee:>8}  {ts}")
            else:
                print(f"  {fills}")
        else:
            print(f"  Response: {j}")

        # Funding payments
        print("\n  FUNDING PAYMENTS")
        print("-" * 70)
        r = auth_get(c, "/api/v3/funding", {"limit": "10", "symbol": "BTC-USDT", "side": "ALL"})
        j = r.json()
        if j.get("data"):
            data = j["data"]
            funding = data.get("fundingValues", data) if isinstance(data, dict) else data
            if isinstance(funding, list):
                if not funding:
                    print("  No funding payments")
                for f in funding:
                    sym = f.get("symbol", "?")
                    rate = f.get("rate", f.get("fundingRate", "?"))
                    pay = f.get("payment", f.get("fundingValue", "?"))
                    ts = f.get("effectiveAt", f.get("datetime", "?"))
                    pos_size = f.get("positionSize", f.get("size", "?"))
                    print(f"  {sym:<14} Rate: {rate:>14}  Payment: {pay:>12}  Size: {pos_size:>10}  {ts}")
            else:
                for k, v in data.items() if isinstance(data, dict) else []:
                    print(f"  {k}: {v}")
        else:
            print(f"  Response: {j}")

        # History orders
        print("\n  ORDER HISTORY (last 5)")
        print("-" * 70)
        r = auth_get(c, "/api/v3/history-orders", {"limit": "5"})
        j = r.json()
        if j.get("data"):
            orders = j["data"].get("orders", j["data"]) if isinstance(j["data"], dict) else j["data"]
            if isinstance(orders, list):
                if not orders:
                    print("  No order history")
                for o in orders:
                    sym = o.get("symbol", "?")
                    side = o.get("side", "?")
                    price = o.get("price", "?")
                    size = o.get("size", "?")
                    status = o.get("status", "?")
                    otype = o.get("type", "?")
                    ts = o.get("createdAt", "?")
                    print(f"  {sym:<14} {side:<6} {otype:<10} {status:<12} "
                          f"Price: {price:>12}  Size: {size:>10}  {ts}")
            else:
                print(f"  {orders}")
        else:
            print(f"  Response: {j}")

        # Historical PnL
        print("\n  HISTORICAL PnL")
        print("-" * 70)
        r = auth_get(c, "/api/v3/historical-pnl", {"limit": "5"})
        j = r.json()
        if j.get("data"):
            data = j["data"]
            if isinstance(data, dict):
                pnls = data.get("historicalPnl", data.get("pnlList", []))
                if isinstance(pnls, list):
                    if not pnls:
                        print("  No PnL history")
                    for p in pnls:
                        print(f"  {p}")
                else:
                    for k, v in data.items():
                        print(f"  {k}: {v}")
            else:
                print(f"  {data}")
        else:
            print(f"  Response: {j}")

        # Transfers
        print("\n  TRANSFERS (deposits/withdrawals)")
        print("-" * 70)
        r = auth_get(c, "/api/v3/transfers", {"limit": "5"})
        j = r.json()
        if j.get("data"):
            data = j["data"]
            transfers = data.get("transfers", data) if isinstance(data, dict) else data
            if isinstance(transfers, list):
                if not transfers:
                    print("  No transfers")
                for t in transfers:
                    ttype = t.get("type", t.get("transferType", "?"))
                    amt = t.get("creditAmount", t.get("amount", "?"))
                    asset = t.get("creditAsset", t.get("asset", "?"))
                    status = t.get("status", "?")
                    ts = t.get("createdAt", t.get("updatedAt", "?"))
                    print(f"  {ttype:<12} {amt:>14} {asset:<6} Status: {status:<12} {ts}")
            elif isinstance(transfers, dict):
                for k, v in transfers.items():
                    print(f"  {k}: {v}")
        else:
            print(f"  Response: {j}")

        print("\n" + "=" * 70)
        print("  Done.")


if __name__ == "__main__":
    main()
