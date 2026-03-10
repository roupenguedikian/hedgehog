#!/usr/bin/env python3
"""
Aster DEX dashboard — funding rates (top 10) + account data.
Reads credentials from .env file.
"""
import asyncio
import hashlib
import hmac
import os
import sys
import time
import httpx
from datetime import datetime, timezone
from urllib.parse import urlencode

# Load .env
def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"')
            os.environ.setdefault(k.strip(), v)

load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE = "https://fapi.asterdex.com"
API_KEY = os.environ.get("ASTER_API_KEY", "")
API_SECRET = os.environ.get("ASTER_API_SECRET", "")
TOP_10 = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI"]

def sign_v1(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params

def ts(ms):
    if not ms:
        return "N/A"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

async def run():
    headers = {"Content-Type": "application/json", "X-MBX-APIKEY": API_KEY}
    async with httpx.AsyncClient(base_url=BASE, timeout=15.0, headers=headers) as c:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n{'=' * 92}")
        print(f"{'ASTER DASHBOARD — ' + now:^92}")
        print(f"{'=' * 92}")

        # 1) Funding rates
        resp = await c.get("/fapi/v1/premiumIndex")
        resp.raise_for_status()
        all_data = resp.json()
        top10 = [d for d in all_data if d.get("symbol") in [s + "USDT" for s in TOP_10]]

        print(f"\n{'FUNDING RATES — TOP 10':^92}")
        print("-" * 92)
        print(f'{"Symbol":<12} {"Last Rate":>12} {"Annualized":>12} {"Mark Price":>14} {"Index Price":>14} {"Next Funding":>22}')
        print("-" * 92)
        for d in sorted(top10, key=lambda x: abs(float(x.get("lastFundingRate", 0))), reverse=True):
            rate = float(d.get("lastFundingRate", 0))
            ann = rate * 1095
            mark = float(d.get("markPrice", 0))
            idx = float(d.get("indexPrice", 0))
            print(f'{d["symbol"]:<12} {rate:>12.6f} {ann * 100:>11.2f}% {mark:>14,.2f} {idx:>14,.2f} {ts(d.get("nextFundingTime","")):>22}')
        print(f"Total symbols: {len(all_data)}")

        # 2) Balance
        resp2 = await c.get("/fapi/v2/balance", params=sign_v1({}))
        if resp2.status_code == 200:
            balances = resp2.json()
            nonzero = [b for b in balances if float(b.get("walletBalance", 0)) != 0]
            print(f"\n{'BALANCES':^92}")
            print("-" * 70)
            print(f'{"Asset":<10} {"Wallet Bal":>14} {"Available":>14} {"Cross UnPnL":>14}')
            print("-" * 70)
            for b in nonzero:
                print(f'{b["asset"]:<10} {float(b["walletBalance"]):>14.4f} {float(b["availableBalance"]):>14.4f} {float(b.get("crossUnPnl", 0)):>14.4f}')
            if not nonzero:
                print("  (no non-zero balances)")

        # 3) Account info
        resp3 = await c.get("/fapi/v4/account", params=sign_v1({}))
        if resp3.status_code == 200:
            acct = resp3.json()
            print(f"\n{'ACCOUNT SUMMARY':^92}")
            print("-" * 60)
            print(f"  Total Wallet Bal:  {acct.get('totalWalletBalance', 'N/A')}")
            print(f"  Total UnPnL:       {acct.get('totalUnrealizedProfit', 'N/A')}")
            print(f"  Total Margin Bal:  {acct.get('totalMarginBalance', 'N/A')}")
            print(f"  Available Bal:     {acct.get('availableBalance', 'N/A')}")
            print(f"  Position Margin:   {acct.get('totalPositionInitialMargin', 'N/A')}")

            positions = acct.get("positions", [])
            open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
            if open_pos:
                print(f"\n{'OPEN POSITIONS (' + str(len(open_pos)) + ')':^92}")
                print("-" * 92)
                print(f'  {"Symbol":<14} {"Side":<6} {"Size":>12} {"Entry":>12} {"Mark":>12} {"UnPnL":>12} {"Liq Price":>12} {"Lev":>5}')
                print("  " + "-" * 85)
                for p in open_pos:
                    amt = float(p.get("positionAmt", 0))
                    side = "LONG" if amt > 0 else "SHORT"
                    print(f'  {p["symbol"]:<14} {side:<6} {abs(amt):>12.4f} {float(p.get("entryPrice", 0)):>12.4f} '
                          f'{float(p.get("markPrice", 0)):>12.4f} {float(p.get("unrealizedProfit", 0)):>12.4f} '
                          f'{float(p.get("liquidationPrice", 0)):>12.4f} {p.get("leverage", ""):>5}')
            else:
                print("\n  No open positions.")

        # 4) Open orders
        resp4 = await c.get("/fapi/v1/openOrders", params=sign_v1({}))
        if resp4.status_code == 200:
            orders = resp4.json()
            if orders:
                print(f"\n{'OPEN ORDERS (' + str(len(orders)) + ')':^92}")
                print("-" * 92)
                for o in orders:
                    print(f'  {o["symbol"]:<14} {o["side"]:<6} {o["type"]:<10} px={float(o.get("price", 0)):.2f} qty={float(o.get("origQty", 0)):.4f} {o.get("status", "")}')
            else:
                print("\n  No open orders.")

        # 5) Recent income
        resp5 = await c.get("/fapi/v1/income", params=sign_v1({"limit": 10}))
        if resp5.status_code == 200:
            income = resp5.json()
            if income:
                print(f"\n{'RECENT INCOME (last 10)':^92}")
                print("-" * 82)
                print(f'  {"Time":>20} {"Type":<24} {"Symbol":<14} {"Amount":>14}')
                print("  " + "-" * 76)
                for i in income:
                    print(f'  {ts(i.get("time", 0)):>20} {i.get("incomeType", ""):<24} {i.get("symbol", ""):<14} {float(i.get("income", 0)):>14.6f}')

        print(f"\n{'=' * 92}\n")

if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("ERROR: ASTER_API_KEY and ASTER_API_SECRET must be set in .env")
        sys.exit(1)
    asyncio.run(run())
