#!/usr/bin/env python3
"""
Apex Omni query tool — used by the /apex Claude skill.
Usage: python3 scripts/apex_query.py [funding|account|positions|orders|fills|history|transfers|all]
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sys
import time
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

BASE = "https://omni.apex.exchange"
API_KEY = os.environ.get("APEX_OMNI_API_KEY", "")
API_SECRET = os.environ.get("APEX_OMNI_API_SECRET", "")
PASSPHRASE = os.environ.get("APEX_OMNI_PASSPHRASE", "")

HAS_AUTH = bool(API_KEY and API_SECRET and PASSPHRASE)


# ── Auth helpers ─────────────────────────────────────────────────────

def sign_request(secret: str, timestamp: str, method: str,
                 request_path: str, data_string: str = "") -> str:
    """HMAC-SHA256 signature matching the apexomni SDK convention."""
    message = timestamp + method.upper() + request_path + data_string
    hmac_key = base64.standard_b64encode(secret.encode("utf-8"))
    sig = hmac.new(hmac_key, message.encode("utf-8"), hashlib.sha256)
    return base64.standard_b64encode(sig.digest()).decode()


def auth_get(client: httpx.AsyncClient, path: str,
             params: dict | None = None) -> httpx.Response:
    """Authenticated GET — query params are embedded in the signed path."""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
        full_path = f"{path}?{qs}" if qs else path
    else:
        full_path = path
    timestamp = str(int(round(time.time() * 1000)))
    sig = sign_request(API_SECRET, timestamp, "GET", full_path)
    headers = {
        "APEX-SIGNATURE": sig,
        "APEX-API-KEY": API_KEY,
        "APEX-TIMESTAMP": timestamp,
        "APEX-PASSPHRASE": PASSPHRASE,
    }
    return client.get(BASE + path, headers=headers, params=params)


def ts(ms_or_iso):
    """Convert epoch-ms or ISO string to readable timestamp."""
    if not ms_or_iso:
        return "N/A"
    if isinstance(ms_or_iso, str):
        try:
            return ms_or_iso[:19].replace("T", " ")
        except Exception:
            return str(ms_or_iso)
    try:
        return datetime.fromtimestamp(
            int(ms_or_iso) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms_or_iso)


# ═══════════════════════════════════════════════════════════════
# PUBLIC — no auth needed
# ═══════════════════════════════════════════════════════════════

async def funding(client):
    """Funding rates for all available symbols + extreme funding scanner."""
    print("=" * 80)
    print("  Apex Omni — FUNDING RATES")
    print("=" * 80)

    # Gather tickers for all known symbols
    symbols = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT",
        "LINKUSDT", "ARBUSDT", "AVAXUSDT", "WIFUSDT", "NEARUSDT",
        "AAVEUSDT", "XRPUSDT", "BNBUSDT", "TONUSDT", "ADAUSDT",
        "MATICUSDT", "OPUSDT", "APTUSDT", "TRXUSDT", "LTCUSDT",
        "DOTUSDT", "SEIUSDT", "PEPEUSDT", "ONDOUSDT", "HYPEUSDT",
        "JUPUSDT", "ORDIUSDT", "TIAUSDT", "STXUSDT", "MKRUSDT",
    ]
    assets = []
    for sym in symbols:
        try:
            resp = await client.get(f"{BASE}/api/v3/ticker", params={"symbol": sym})
            items = resp.json().get("data", [])
            if items and isinstance(items, list) and items[0]:
                d = items[0]
                rate = float(d.get("fundingRate") or 0)
                pred = float(d.get("predictedFundingRate") or 0)
                last = float(d.get("lastPrice") or 0)
                hi = float(d.get("highPrice24h") or 0)
                lo = float(d.get("lowPrice24h") or 0)
                oi = float(d.get("openInterest") or 0)
                chg = float(d.get("price24hPcnt") or 0)
                idx = float(d.get("indexPrice") or 0)
                next_t = d.get("nextFundingTime", "")
                ann = rate * (8760 / 8)  # 8h cycle → annualized
                oi_usd = oi * last
                assets.append({
                    "symbol": sym, "rate": rate, "predicted": pred,
                    "annualized": ann, "last": last, "hi": hi, "lo": lo,
                    "oi": oi, "oi_usd": oi_usd, "chg": chg, "index": idx,
                    "next_funding": next_t,
                })
        except Exception:
            pass

    # Sort by OI (proxy for importance since we don't have 24h volume here)
    by_oi = sorted(assets, key=lambda a: a["oi_usd"], reverse=True)

    print(f"\n  {'SYMBOL':<12} {'RATE/8H':>12} {'ANNUAL':>8} {'PREDICTED':>12} "
          f"{'LAST':>12} {'24H CHG':>8} {'OI (USD)':>14}")
    print("  " + "-" * 85)
    for a in by_oi:
        print(f"  {a['symbol']:<12} {a['rate']*100:>11.6f}% {a['annualized']*100:>+7.2f}% "
              f"{a['predicted']*100:>11.6f}% ${a['last']:>11,.2f} "
              f"{a['chg']*100:>+7.2f}% ${a['oi_usd']:>13,.0f}")

    if assets:
        next_t = assets[0].get("next_funding", "")
        if next_t:
            print(f"\n  Next funding: {next_t}")

    # Extreme funding
    by_rate = sorted(assets, key=lambda a: a["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':<12} {'ANNUAL':>8} {'LAST':>12} {'OI (USD)':>14}")
    print("  " + "-" * 50)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:<12} {a['annualized']*100:>+7.2f}% "
              f"${a['last']:>11,.2f} ${a['oi_usd']:>13,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:<12} {a['annualized']*100:>+7.2f}% "
              f"${a['last']:>11,.2f} ${a['oi_usd']:>13,.0f}")


# ═══════════════════════════════════════════════════════════════
# AUTHENTICATED
# ═══════════════════════════════════════════════════════════════

async def account(client):
    """Account balance and margin info."""
    print("=" * 80)
    print("  Apex Omni — ACCOUNT BALANCE")
    print("=" * 80)

    # V3 account (identity + l2Key)
    resp = await auth_get(client, "/api/v3/account")
    j = resp.json()
    data = j.get("data", {})
    if data:
        print(f"\n  Account ID:    {data.get('id', 'N/A')}")
        print(f"  Address:       {data.get('ethereumAddress', 'N/A')}")
        print(f"  Maker Fee:     {data.get('makerFeeRate', 'N/A')}")
        print(f"  Taker Fee:     {data.get('takerFeeRate', 'N/A')}")
    else:
        code = j.get("code", "")
        msg = j.get("msg", "")
        print(f"\n  Account endpoint: code={code} msg={msg}")

    # V3 account-balance (the real numbers)
    resp2 = await auth_get(client, "/api/v3/account-balance")
    j2 = resp2.json()
    bal = j2.get("data", {})
    if bal and isinstance(bal, dict) and j2.get("code") is None:
        print(f"\n  {'METRIC':<30s} {'VALUE':>16s}")
        print("  " + "-" * 48)
        fields = [
            ("Total Equity", "totalEquityValue"),
            ("Available Balance", "availableBalance"),
            ("Total Available", "totalAvailableBalance"),
            ("Wallet Balance", "walletBalance"),
            ("Initial Margin", "initialMargin"),
            ("Maintenance Margin", "maintenanceMargin"),
            ("Unrealized PnL", "unrealizedPnl"),
            ("Realized PnL", "realizedPnl"),
            ("Total Risk", "totalRisk"),
            ("Liabilities", "liabilities"),
        ]
        for label, key in fields:
            val = bal.get(key, "")
            if val == "" or val is None:
                continue
            try:
                print(f"  {label:<30s} ${float(val):>15,.2f}")
            except (ValueError, TypeError):
                print(f"  {label:<30s} {val:>16s}")
    else:
        code = j2.get("code", "")
        msg = j2.get("msg", "")
        if code:
            print(f"\n  Balance endpoint: code={code} msg={msg}")


async def positions(client):
    """Open positions from account data."""
    print("=" * 80)
    print("  Apex Omni — OPEN POSITIONS")
    print("=" * 80)

    # The V3 account endpoint includes openPositions in the nested account
    resp = await auth_get(client, "/api/v3/account")
    j = resp.json()
    data = j.get("data", {})

    acct = data.get("account", data.get("accounts", {}))
    open_pos = {}
    if isinstance(acct, dict):
        open_pos = acct.get("openPositions", {})
    elif isinstance(acct, list) and acct:
        open_pos = acct[0].get("openPositions", {})

    if not open_pos:
        # Try V3 account-balance which may have position-level data
        resp2 = await auth_get(client, "/api/v3/account-balance")
        j2 = resp2.json()
        bal = j2.get("data", {})
        # Check if there's a positions array
        if isinstance(bal, dict):
            pos_list = bal.get("positions", bal.get("openPositions", []))
            if isinstance(pos_list, list) and pos_list:
                print(f"\n  {'SYMBOL':<14} {'SIDE':<6} {'SIZE':>10} {'ENTRY':>12} "
                      f"{'uPnL':>12} {'LIQ':>12}")
                print("  " + "-" * 70)
                total_upnl = 0
                for p in pos_list:
                    sym = p.get("symbol", "?")
                    side = p.get("side", "?")
                    size = p.get("size", p.get("qty", "0"))
                    entry = p.get("entryPrice", p.get("avgEntryPrice", "?"))
                    upnl = float(p.get("unrealizedPnl", 0))
                    liq = p.get("liquidationPrice", p.get("liqPrice", "N/A"))
                    total_upnl += upnl
                    print(f"  {sym:<14} {side:<6} {size:>10} ${float(entry):>11,.2f} "
                          f"${upnl:>+11,.2f} {liq:>12}")
                print(f"\n  Total uPnL: ${total_upnl:+,.2f}")
                return

        print("\n  No open positions")
        return

    if isinstance(open_pos, dict) and open_pos:
        print(f"\n  {'SYMBOL':<14} {'SIDE':<6} {'SIZE':>10} {'ENTRY':>12} "
              f"{'uPnL':>12} {'LIQ':>12}")
        print("  " + "-" * 70)
        total_upnl = 0
        for sym, pos in open_pos.items():
            side = pos.get("side", "?")
            size = pos.get("size", "0")
            entry = pos.get("entryPrice", "?")
            upnl = float(pos.get("unrealizedPnl", 0))
            liq = pos.get("liquidationPrice", "N/A")
            total_upnl += upnl
            print(f"  {sym:<14} {side:<6} {size:>10} ${float(entry):>11,.2f} "
                  f"${upnl:>+11,.2f} {liq:>12}")
        print(f"\n  Total uPnL: ${total_upnl:+,.2f}")
    else:
        print("\n  No open positions")


async def orders(client):
    """Open orders."""
    print("=" * 80)
    print("  Apex Omni — OPEN ORDERS")
    print("=" * 80)

    resp = await auth_get(client, "/api/v3/open-orders")
    j = resp.json()
    data = j.get("data", [])
    order_list = data.get("orders", data) if isinstance(data, dict) else data

    if not order_list or (isinstance(order_list, list) and len(order_list) == 0):
        print("\n  No open orders")
        return

    print(f"\n  {'SYMBOL':<14} {'SIDE':<6} {'TYPE':<10} {'PRICE':>12} "
          f"{'SIZE':>10} {'FILLED':>10} {'STATUS':<12}")
    print("  " + "-" * 80)
    for o in order_list:
        sym = o.get("symbol", "?")
        side = o.get("side", "?")
        otype = o.get("orderType", o.get("type", "?"))
        price = o.get("price", "?")
        size = o.get("size", "?")
        filled = o.get("filledSize", o.get("cumMatchFillSize", "0"))
        status = o.get("status", "?")
        print(f"  {sym:<14} {side:<6} {otype:<10} ${float(price):>11,.2f} "
              f"{size:>10} {filled:>10} {status:<12}")


async def fills(client):
    """Recent trade fills."""
    print("=" * 80)
    print("  Apex Omni — RECENT FILLS (last 20)")
    print("=" * 80)

    resp = await auth_get(client, "/api/v3/fills", {"limit": "20"})
    j = resp.json()
    data = j.get("data", {})
    fill_list = data.get("orders", data) if isinstance(data, dict) else data

    if isinstance(fill_list, list) and len(fill_list) == 0:
        print("\n  No recent fills")
        return

    if isinstance(fill_list, list):
        print(f"\n  {'TIME':>20} {'SYMBOL':<14} {'SIDE':<6} {'TYPE':<8} "
              f"{'PRICE':>12} {'SIZE':>10} {'FEE':>10} {'DIR':<6}")
        print("  " + "-" * 90)
        for f in fill_list:
            sym = f.get("symbol", "?")
            side = f.get("side", "?")
            price = f.get("price", "?")
            size = f.get("size", "?")
            fee = f.get("fee", "?")
            direction = f.get("direction", "?")
            otype = f.get("orderType", "?")
            created = ts(f.get("createdAt", 0))
            try:
                fee_str = f"${float(fee):>9,.4f}"
            except (ValueError, TypeError):
                fee_str = f"{fee:>10}"
            print(f"  {created:>20} {sym:<14} {side:<6} {otype:<8} "
                  f"${float(price):>11,.2f} {size:>10} {fee_str} {direction:<6}")

        # Total fees
        total_fees = sum(float(f.get("fee", 0)) for f in fill_list)
        print(f"\n  Total fees (shown): ${total_fees:,.4f}")
    else:
        print(f"\n  {fill_list}")


async def history(client):
    """Order history."""
    print("=" * 80)
    print("  Apex Omni — ORDER HISTORY (last 20)")
    print("=" * 80)

    resp = await auth_get(client, "/api/v3/history-orders", {"limit": "20"})
    j = resp.json()
    data = j.get("data", {})
    order_list = data.get("orders", data) if isinstance(data, dict) else data

    if isinstance(order_list, list) and len(order_list) == 0:
        print("\n  No order history")
        return

    if isinstance(order_list, list):
        print(f"\n  {'TIME':>20} {'SYMBOL':<14} {'SIDE':<6} {'TYPE':<10} "
              f"{'STATUS':<12} {'PRICE':>12} {'SIZE':>10}")
        print("  " + "-" * 90)
        for o in order_list:
            sym = o.get("symbol", "?")
            side = o.get("side", "?")
            otype = o.get("orderType", o.get("type", "?"))
            status = o.get("status", "?")
            price = o.get("price", "?")
            size = o.get("size", "?")
            created = ts(o.get("createdAt", 0))
            print(f"  {created:>20} {sym:<14} {side:<6} {otype:<10} "
                  f"{status:<12} ${float(price):>11,.2f} {size:>10}")
    else:
        print(f"\n  {order_list}")


async def pnl(client):
    """Historical PnL (closed positions)."""
    print("=" * 80)
    print("  Apex Omni — HISTORICAL PnL (last 20)")
    print("=" * 80)

    resp = await auth_get(client, "/api/v3/historical-pnl", {"limit": "20"})
    j = resp.json()
    data = j.get("data", {})
    pnl_list = data.get("historicalPnl", data) if isinstance(data, dict) else data

    if isinstance(pnl_list, list) and len(pnl_list) == 0:
        print("\n  No PnL history")
        return

    if isinstance(pnl_list, list):
        print(f"\n  {'TIME':>20} {'SYMBOL':<14} {'SIDE':<6} {'SIZE':>10} "
              f"{'ENTRY':>12} {'EXIT':>12} {'TOTAL PnL':>12}")
        print("  " + "-" * 92)
        total_pnl = 0
        total_fees = 0
        for p in pnl_list:
            sym = p.get("symbol", "?")
            side = p.get("side", "?")
            size = p.get("size", "?")
            entry = p.get("price", "?")
            exit_p = p.get("exitPrice", "?")
            pnl_val = float(p.get("totalPnl", 0))
            fee = float(p.get("fee", 0))
            created = ts(p.get("createdAt", 0))
            total_pnl += pnl_val
            total_fees += fee
            print(f"  {created:>20} {sym:<14} {side:<6} {size:>10} "
                  f"${float(entry):>11,.2f} ${float(exit_p):>11,.2f} ${pnl_val:>+11,.4f}")

        print(f"\n  Net PnL (shown):  ${total_pnl:+,.4f}")
        print(f"  Total fees:       ${total_fees:,.4f}")
    else:
        print(f"\n  {pnl_list}")


async def income(client):
    """Funding payments received/paid on positions."""
    print("=" * 80)
    print("  Apex Omni — FUNDING PAYMENTS")
    print("=" * 80)

    resp = await auth_get(client, "/api/v3/funding", {"limit": "50"})
    j = resp.json()
    data = j.get("data", {})
    funding_list = data.get("fundingValues", data) if isinstance(data, dict) else data

    if isinstance(funding_list, list) and funding_list:
        print(f"\n  {'TIME':>20} {'SYMBOL':<14} {'SIDE':<6} {'SIZE':>10} "
              f"{'RATE':>14} {'PAYMENT':>12}")
        print("  " + "-" * 82)
        total_payment = 0
        by_symbol = {}
        for f in funding_list:
            sym = f.get("symbol", "?")
            side = f.get("side", "?")
            size = f.get("positionSize", "?")
            rate = f.get("rate", "?")
            payment = float(f.get("fundingValue", 0))
            created = ts(f.get("fundingTime", 0))
            total_payment += payment
            by_symbol[sym] = by_symbol.get(sym, 0) + payment
            try:
                rate_str = f"{float(rate)*100:>13.6f}%"
            except (ValueError, TypeError):
                rate_str = f"{rate:>14}"
            print(f"  {created:>20} {sym:<14} {side:<6} {size:>10} "
                  f"{rate_str} ${payment:>+11,.4f}")

        print(f"\n  Net funding received: ${total_payment:+,.4f}")
        if len(by_symbol) > 1:
            print(f"  By symbol:")
            for sym, val in sorted(by_symbol.items(), key=lambda x: x[1]):
                print(f"    {sym:<14} ${val:+,.4f}")
    elif isinstance(funding_list, list):
        print("\n  No funding payments")
    else:
        code = j.get("code", "")
        msg = j.get("msg", "")
        print(f"\n  Funding endpoint: code={code} msg={msg}")

    # Also show funding rate history for major symbols (public)
    print(f"\n  RECENT FUNDING RATE HISTORY (public)")
    print("  " + "-" * 70)
    for sym in ["BTC-USDT", "ETH-USDT", "SOL-USDT"]:
        try:
            resp = await client.get(
                f"{BASE}/api/v3/history-funding",
                params={"symbol": sym, "limit": "5"},
            )
            hdata = resp.json().get("data", {})
            entries = hdata.get("historyFunds", [])
            if entries:
                print(f"\n  {sym}:")
                for e in entries:
                    rate = float(e.get("rate", 0))
                    price = e.get("price", "?")
                    ftime = ts(e.get("fundingTime", 0))
                    ann = rate * 1095 * 100
                    print(f"    {ftime}  Rate: {rate*100:>11.6f}%  Ann: {ann:>+7.2f}%  Price: ${float(price):>11,.2f}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {"funding": funding}
AUTH_SECTIONS = {
    "account": account,
    "positions": positions,
    "orders": orders,
    "fills": fills,
    "history": history,
    "pnl": pnl,
    "income": income,
}
ALL_SECTIONS = {**PUBLIC_SECTIONS, **AUTH_SECTIONS}


async def main():
    arg = sys.argv[1].lower().strip() if len(sys.argv) > 1 else "all"

    async with httpx.AsyncClient(timeout=15.0) as client:
        if arg == "all":
            await funding(client)
            print()
            if HAS_AUTH:
                for name, fn in AUTH_SECTIONS.items():
                    try:
                        await fn(client)
                    except Exception as e:
                        print(f"\n  {name}: Error — {e}")
                    print()
            else:
                print("  [Auth sections skipped — set APEX_OMNI_API_KEY, "
                      "APEX_OMNI_API_SECRET, APEX_OMNI_PASSPHRASE in .env]")
        elif arg in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[arg](client)
        elif arg in AUTH_SECTIONS:
            if not HAS_AUTH:
                print(f"ERROR: {arg} requires APEX_OMNI_API_KEY, "
                      "APEX_OMNI_API_SECRET, APEX_OMNI_PASSPHRASE in .env")
                sys.exit(1)
            await AUTH_SECTIONS[arg](client)
        else:
            print(f"Unknown section: {arg}")
            print(f"Valid options: {', '.join(ALL_SECTIONS.keys())}, all")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
