#!/usr/bin/env python3
"""
Hedgehog Account Snapshot — balance, margin, positions, orders across all venues.
Runs all venue queries in parallel, prints per-venue details + system totals.

Usage:
    python3 scripts/exchanges/snapshot.py             # full snapshot
    python3 scripts/exchanges/snapshot.py --brief     # account summary only (no position details)
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

# ── Load .env ────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))


def _load_env():
    path = os.path.join(_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_env()

# ANSI colors
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
D = "\033[2m"
B = "\033[1m"
X = "\033[0m"


# ── Snapshot data structure ──────────────────────────────────────────────

def _snap(venue: str, **kw) -> dict:
    return {
        "venue": venue,
        "equity": kw.get("equity", 0.0),
        "wallet_balance": kw.get("wallet_balance", 0.0),
        "margin_used": kw.get("margin_used", 0.0),
        "margin_free": kw.get("margin_free", 0.0),
        "margin_util_pct": kw.get("margin_util_pct", 0.0),
        "unrealized_pnl": kw.get("unrealized_pnl", 0.0),
        "position_notional": kw.get("position_notional", 0.0),
        "positions": kw.get("positions", []),
        "num_orders": kw.get("num_orders", 0),
        "num_fills": kw.get("num_fills", 0),
        "total_fees": kw.get("total_fees", 0.0),
        "error": kw.get("error"),
    }


# ═════════════════════════════════════════════════════════════════════════
# VENUE FETCHERS — each returns a standardized snapshot dict
# ═════════════════════════════════════════════════════════════════════════


async def fetch_hyperliquid() -> dict:
    address = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "")
    if not address:
        return _snap("Hyperliquid", error="HYPERLIQUID_WALLET_ADDRESS not set")

    API = "https://api.hyperliquid.xyz/info"
    async with httpx.AsyncClient(timeout=15) as c:
        # Account + positions
        raw = (await c.post(API, json={"type": "clearinghouseState", "user": address})).json()
        cross = raw.get("crossMarginSummary", {})
        equity = float(cross.get("accountValue", 0))
        wallet = float(cross.get("totalRawUsd", 0))
        margin_used = float(cross.get("totalMarginUsed", 0))
        margin_free = equity - margin_used
        util = (margin_used / equity * 100) if equity else 0

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for item in raw.get("assetPositions", []):
            p = item.get("position", item)
            sz = float(p.get("szi", 0))
            if sz == 0:
                continue
            ntl = abs(float(p.get("positionValue", 0)))
            u = float(p.get("unrealizedPnl", 0))
            upnl += u
            total_ntl += ntl
            lev = p.get("leverage", {})
            lev_val = lev.get("value", "?") if isinstance(lev, dict) else lev
            positions.append({
                "symbol": p["coin"], "side": "LONG" if sz > 0 else "SHORT",
                "size": abs(sz), "notional": ntl,
                "entry": float(p.get("entryPx", 0)),
                "mark": ntl / abs(sz) if sz else 0,
                "upnl": u, "leverage": lev_val,
            })

        # Orders
        ords = (await c.post(API, json={"type": "openOrders", "user": address})).json()

        # Fills
        fills = (await c.post(API, json={"type": "userFills", "user": address})).json()
        recent = fills[-20:] if fills else []
        total_fees = sum(abs(float(f.get("fee", 0))) for f in recent)

        return _snap("Hyperliquid", equity=equity, wallet_balance=wallet,
                      margin_used=margin_used, margin_free=margin_free,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions,
                      num_orders=len(ords), num_fills=len(recent),
                      total_fees=total_fees)


async def fetch_aster() -> dict:
    api_key = os.getenv("ASTER_API_KEY", "")
    api_secret = os.getenv("ASTER_API_SECRET", "")
    if not (api_key and api_secret):
        return _snap("Aster", error="ASTER_API_KEY / ASTER_API_SECRET not set")

    BASE = "https://fapi.asterdex.com"

    def sign(params):
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        qs = urlencode(params)
        sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    headers = {"Content-Type": "application/json", "X-MBX-APIKEY": api_key}
    async with httpx.AsyncClient(base_url=BASE, timeout=15, headers=headers) as c:
        # Account + positions
        acct = (await c.get("/fapi/v4/account", params=sign({}))).json()
        margin_bal = float(acct.get("totalMarginBalance") or 0)
        wallet = float(acct.get("totalWalletBalance") or 0)
        available = float(acct.get("availableBalance") or 0)
        pos_margin = float(acct.get("totalPositionInitialMargin") or 0)
        order_margin = float(acct.get("totalOpenOrderInitialMargin") or 0)
        margin_used = pos_margin + order_margin
        util = (margin_used / margin_bal * 100) if margin_bal > 0 else 0
        upnl_total = float(acct.get("totalUnrealizedProfit") or 0)

        positions = []
        total_ntl = 0.0
        for p in acct.get("positions", []):
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            ntl = abs(float(p.get("notional", 0)))
            total_ntl += ntl
            positions.append({
                "symbol": p["symbol"].replace("USDT", ""),
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt), "notional": ntl,
                "entry": float(p.get("entryPrice", 0)),
                "mark": float(p.get("markPrice", 0)),
                "upnl": float(p.get("unrealizedProfit", 0)),
                "leverage": p.get("leverage", "?"),
            })

        # Orders
        ords = (await c.get("/fapi/v1/openOrders", params=sign({}))).json()

        # Fills
        fills = (await c.get("/fapi/v1/userTrades", params=sign({"limit": 20}))).json()
        total_fees = sum(abs(float(f.get("commission", 0))) for f in (fills if isinstance(fills, list) else []))

        return _snap("Aster", equity=margin_bal, wallet_balance=wallet,
                      margin_used=margin_used, margin_free=available,
                      margin_util_pct=util, unrealized_pnl=upnl_total,
                      position_notional=total_ntl, positions=positions,
                      num_orders=len(ords) if isinstance(ords, list) else 0,
                      num_fills=len(fills) if isinstance(fills, list) else 0,
                      total_fees=total_fees)


async def fetch_lighter() -> dict:
    acct_idx = os.getenv("LIGHTER_ACCOUNT_INDEX", "")
    if not acct_idx:
        return _snap("Lighter", error="LIGHTER_ACCOUNT_INDEX not set")

    BASE = "https://mainnet.zklighter.elliot.ai"
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        # Account + positions (public endpoint)
        resp = await c.get("/api/v1/account", params={"by": "index", "value": acct_idx})
        resp.raise_for_status()
        acct = resp.json()["accounts"][0]

        collateral = float(acct["collateral"])
        available = float(acct["available_balance"])
        total_asset = float(acct["total_asset_value"])
        margin_used = collateral - available
        util = (margin_used / collateral * 100) if collateral > 0 else 0

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for p in acct["positions"]:
            sz = float(p["position"])
            if abs(sz) < 1e-12:
                continue
            value = float(p["position_value"])
            u = float(p["unrealized_pnl"])
            upnl += u
            total_ntl += value
            mark = value / abs(sz) if abs(sz) > 1e-12 else 0
            imf = float(p["initial_margin_fraction"])
            lev = 100.0 / imf if imf > 0 else 0
            positions.append({
                "symbol": p["symbol"], "side": "SHORT" if p["sign"] == -1 else "LONG",
                "size": abs(sz), "notional": value,
                "entry": float(p["avg_entry_price"]), "mark": mark,
                "upnl": u, "leverage": f"{lev:.0f}",
            })

        # Orders — need auth token
        num_orders = int(acct.get("total_order_count", 0))

        return _snap("Lighter", equity=total_asset, wallet_balance=collateral,
                      margin_used=margin_used, margin_free=available,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions,
                      num_orders=num_orders)


async def fetch_apex() -> dict:
    api_key = os.getenv("APEX_OMNI_API_KEY", "")
    api_secret = os.getenv("APEX_OMNI_API_SECRET", "")
    passphrase = os.getenv("APEX_OMNI_PASSPHRASE", "")
    if not (api_key and api_secret and passphrase):
        return _snap("Apex", error="APEX_OMNI_API_KEY / SECRET / PASSPHRASE not set")

    BASE = "https://omni.apex.exchange"

    def sign_req(ts_str, method, path):
        msg = ts_str + method.upper() + path
        hmac_key = base64.standard_b64encode(api_secret.encode("utf-8"))
        sig = hmac.new(hmac_key, msg.encode("utf-8"), hashlib.sha256)
        return base64.standard_b64encode(sig.digest()).decode()

    def auth_headers(path):
        ts_str = str(int(round(time.time() * 1000)))
        return {
            "APEX-SIGNATURE": sign_req(ts_str, "GET", path),
            "APEX-API-KEY": api_key,
            "APEX-TIMESTAMP": ts_str,
            "APEX-PASSPHRASE": passphrase,
        }

    async with httpx.AsyncClient(timeout=15) as c:
        # Balance
        path = "/api/v3/account-balance"
        resp = await c.get(BASE + path, headers=auth_headers(path))
        bal = resp.json().get("data", {})

        equity = float(bal.get("totalEquityValue", 0) or 0)
        wallet = float(bal.get("walletBalance", 0) or 0) or equity
        available = float(bal.get("totalAvailableBalance", bal.get("availableBalance", 0)) or 0)
        init_margin = float(bal.get("initialMargin", 0) or 0)
        maint_margin = float(bal.get("maintenanceMargin", 0) or 0)
        upnl_total = float(bal.get("unrealizedPnl", 0) or 0)
        margin_used = init_margin
        margin_free = max(available, equity - margin_used)
        util = (margin_used / equity * 100) if equity > 0 else 0

        # Account (for positions)
        path2 = "/api/v3/account"
        resp2 = await c.get(BASE + path2, headers=auth_headers(path2))
        data2 = resp2.json().get("data", {})
        acct_inner = data2.get("account", data2.get("accounts", {}))
        open_pos = {}
        if isinstance(acct_inner, dict):
            open_pos = acct_inner.get("openPositions", {})
        elif isinstance(acct_inner, list) and acct_inner:
            open_pos = acct_inner[0].get("openPositions", {})

        positions = []
        total_ntl = 0.0
        if isinstance(open_pos, dict):
            for sym, pos in open_pos.items():
                sz = float(pos.get("size", 0) or 0)
                if sz == 0:
                    continue
                entry = float(pos.get("entryPrice", 0) or 0)
                ntl = abs(sz) * entry
                total_ntl += ntl
                positions.append({
                    "symbol": sym.replace("USDT", "").replace("-", ""),
                    "side": pos.get("side", "?"),
                    "size": abs(sz), "notional": ntl,
                    "entry": entry,
                    "mark": float(pos.get("markPrice", entry) or entry),
                    "upnl": float(pos.get("unrealizedPnl", 0) or 0),
                    "leverage": pos.get("leverage", "?"),
                })

        # Orders
        path3 = "/api/v3/open-orders"
        resp3 = await c.get(BASE + path3, headers=auth_headers(path3))
        j3 = resp3.json().get("data", [])
        ords = j3.get("orders", j3) if isinstance(j3, dict) else j3
        num_orders = len(ords) if isinstance(ords, list) else 0

        # Fills
        qs = "?limit=20"
        path4 = "/api/v3/fills" + qs
        resp4 = await c.get(BASE + "/api/v3/fills", params={"limit": "20"},
                            headers=auth_headers(path4))
        j4 = resp4.json().get("data", {})
        fill_list = j4.get("orders", j4) if isinstance(j4, dict) else j4
        num_fills = len(fill_list) if isinstance(fill_list, list) else 0
        total_fees = sum(abs(float(f.get("fee", 0))) for f in fill_list) if isinstance(fill_list, list) else 0

        return _snap("Apex", equity=equity, wallet_balance=wallet,
                      margin_used=margin_used, margin_free=margin_free,
                      margin_util_pct=util, unrealized_pnl=upnl_total,
                      position_notional=total_ntl, positions=positions,
                      num_orders=num_orders, num_fills=num_fills,
                      total_fees=total_fees)


async def fetch_dydx() -> dict:
    address = os.getenv("DYDX_WALLET_ADDRESS", "")
    if not address:
        return _snap("dYdX", error="DYDX_WALLET_ADDRESS not set")

    BASE = "https://indexer.dydx.trade/v4"
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        resp = await c.get(f"/addresses/{address}")
        resp.raise_for_status()
        data = resp.json()
        subaccounts = data.get("subaccounts", [])

        equity = 0.0
        margin_used = 0.0
        margin_free = 0.0
        upnl = 0.0
        wallet = 0.0
        positions = []
        total_ntl = 0.0

        # Get oracle prices for notional calc
        mkt_resp = await c.get("/perpetualMarkets")
        mkt_resp.raise_for_status()
        oracle_prices = {}
        for ticker, m in mkt_resp.json().get("markets", {}).items():
            oracle_prices[ticker] = float(m.get("oraclePrice", 0))

        for sa in subaccounts:
            eq = float(sa.get("equity", 0))
            fc = float(sa.get("freeCollateral", 0))
            equity += eq
            margin_free += fc
            margin_used += eq - fc

            # USDC balance
            for _, ap in sa.get("assetPositions", {}).items():
                if ap.get("symbol") == "USDC":
                    sz = float(ap.get("size", 0))
                    wallet += sz if ap.get("side") == "LONG" else -sz

            # Positions
            for market, pp in sa.get("openPerpetualPositions", {}).items():
                size = float(pp.get("size", 0))
                entry = float(pp.get("entryPrice", 0))
                u = float(pp.get("unrealizedPnl", 0))
                mark = oracle_prices.get(market, 0)
                ntl = abs(size) * mark
                upnl += u
                total_ntl += ntl
                positions.append({
                    "symbol": market.replace("-USD", ""),
                    "side": pp.get("side", "?"),
                    "size": abs(size), "notional": ntl,
                    "entry": entry, "mark": mark,
                    "upnl": u, "leverage": "?",
                })

        util = (margin_used / equity * 100) if equity > 0 else 0

        # Orders
        ord_resp = await c.get(f"/orders", params={
            "address": address, "subaccountNumber": "0",
            "status": "OPEN", "limit": "100",
        })
        ords = ord_resp.json() if ord_resp.status_code == 200 else []
        num_orders = len(ords) if isinstance(ords, list) else 0

        # Fills
        fill_resp = await c.get(f"/fills", params={
            "address": address, "subaccountNumber": "0", "limit": "20",
        })
        fills_data = fill_resp.json().get("fills", []) if fill_resp.status_code == 200 else []
        total_fees = sum(abs(float(f.get("fee", 0))) for f in fills_data)

        return _snap("dYdX", equity=equity, wallet_balance=wallet,
                      margin_used=margin_used, margin_free=margin_free,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions,
                      num_orders=num_orders, num_fills=len(fills_data),
                      total_fees=total_fees)


async def fetch_drift() -> dict:
    address = os.getenv("DRIFT_WALLET_ADDRESS", "")
    if not address:
        return _snap("Drift", error="DRIFT_WALLET_ADDRESS not set")

    DATA_API = "https://data.api.drift.trade"
    async with httpx.AsyncClient(timeout=20) as c:
        # Resolve account ID
        resp = await c.get(f"{DATA_API}/authority/{address}/accounts")
        resp.raise_for_status()
        accounts = resp.json().get("accounts", [])
        if not accounts:
            return _snap("Drift", error="Account not found on Drift")
        account_id = accounts[0]["accountId"]

        # Account + positions + orders
        resp2 = await c.get(f"{DATA_API}/user/{account_id}")
        resp2.raise_for_status()
        data = resp2.json()

        acct = data.get("account", {})
        collateral = float(acct.get("totalCollateral", 0))
        free_col = float(acct.get("freeCollateral", 0))
        margin_used = collateral - free_col
        util = (margin_used / collateral * 100) if collateral > 0 else 0
        balance = float(acct.get("balance", 0))

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for p in data.get("positions", []):
            sz = float(p.get("baseAssetAmount", p.get("size", 0)))
            if sz == 0:
                continue
            mark = float(p.get("markPrice", p.get("oraclePrice", 0)))
            ntl = abs(sz) * mark
            u = float(p.get("unrealizedPnl", p.get("pnl", 0)))
            upnl += u
            total_ntl += ntl
            lev = p.get("leverage")
            positions.append({
                "symbol": p.get("symbol", p.get("marketName", "?")),
                "side": "LONG" if sz > 0 else "SHORT",
                "size": abs(sz), "notional": ntl,
                "entry": float(p.get("entryPrice", p.get("avgEntryPrice", 0))),
                "mark": mark, "upnl": u,
                "leverage": f"{float(lev):.0f}" if lev else "?",
            })

        num_orders = len(data.get("orders", []))

        # Fills
        fill_resp = await c.get(f"{DATA_API}/user/{account_id}/trades", params={"limit": 20})
        fills = fill_resp.json().get("records", []) if fill_resp.status_code == 200 else []
        total_fees = 0.0
        for rec in fills:
            if "userFee" in rec:
                total_fees += abs(float(rec["userFee"]))
            elif rec.get("taker") == account_id:
                total_fees += abs(float(rec.get("takerFee", 0)))
            else:
                total_fees += abs(float(rec.get("makerFee", 0)))

        return _snap("Drift", equity=collateral, wallet_balance=balance,
                      margin_used=margin_used, margin_free=free_col,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions,
                      num_orders=num_orders, num_fills=len(fills),
                      total_fees=total_fees)


async def fetch_edgex() -> dict:
    account_id = os.getenv("EDGEX_ACCOUNT_ID", "")
    private_key = os.getenv("EDGEX_PRIVATE_KEY", "")
    if not (account_id and private_key):
        return _snap("EdgeX", error="EDGEX_ACCOUNT_ID / EDGEX_PRIVATE_KEY not set")

    try:
        from eth_account import Account
    except ImportError:
        return _snap("EdgeX", error="eth_account not installed (pip install eth-account)")

    BASE = "https://pro.edgex.exchange"

    def sign_req(method, path, params=None):
        ts_str = str(int(time.time() * 1000))
        param_str = urlencode(sorted(params.items())) if params else ""
        message = ts_str + method.upper() + path + param_str
        msg_hash = hashlib.sha3_256(message.encode()).digest()
        acct = Account.from_key(private_key)
        sig = acct.unsafe_sign_hash(msg_hash)
        return {
            "X-edgeX-Api-Timestamp": ts_str,
            "X-edgeX-Api-Signature": sig.signature.hex(),
        }

    async with httpx.AsyncClient(timeout=15) as c:
        path = "/api/v1/private/account/getAccountAsset"
        params = {"accountId": account_id}
        headers = sign_req("GET", path, params)
        resp = await c.get(f"{BASE}{path}", params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != "SUCCESS":
            return _snap("EdgeX", error=f"API: {body.get('msg', body.get('code'))}")

        data = body["data"]
        total_equity = float(data.get("totalEquityValue", 0))
        available = float(data.get("availableAmount", 0))
        margin_used = total_equity - available
        util = (margin_used / total_equity * 100) if total_equity > 0 else 0

        # Metadata for symbol names
        meta_resp = await c.get(f"{BASE}/api/v1/public/meta/getMetaData")
        meta_resp.raise_for_status()
        meta = meta_resp.json()["data"]
        sym_re = __import__("re").compile(r"^(1000(?:PEPE|SATS|SHIB|BONK|FLOKI)|[A-Z0-9]+?)2?USD$")
        contracts = {}
        for cn in meta["contractList"]:
            name = cn["contractName"]
            if name.startswith("TEMP"):
                continue
            m = sym_re.match(name)
            contracts[cn["contractId"]] = m.group(1) if m else name.replace("USD", "")

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for p in data.get("positionAssetList", []):
            sz = float(p.get("openSize", 0))
            if abs(sz) < 1e-12:
                continue
            value = abs(float(p.get("openValue", 0)))
            u = float(p.get("unrealizePnl", 0))
            upnl += u
            total_ntl += value
            entry = abs(float(p.get("openValue", 0)) / sz) if sz != 0 else 0
            cid = p.get("contractId", "")
            positions.append({
                "symbol": contracts.get(cid, cid),
                "side": "LONG" if sz > 0 else "SHORT",
                "size": abs(sz), "notional": value,
                "entry": entry,
                "mark": float(p.get("markPrice", entry)),
                "upnl": u, "leverage": "?",
            })

        return _snap("EdgeX", equity=total_equity, wallet_balance=total_equity - upnl,
                      margin_used=margin_used, margin_free=available,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions)


async def fetch_paradex() -> dict:
    l2_addr = os.getenv("PARADEX_L2_ADDRESS", "")
    l2_key = os.getenv("PARADEX_L2_PRIVATE_KEY", "")
    if not (l2_addr and l2_key):
        return _snap("Paradex", error="PARADEX_L2_ADDRESS / PARADEX_L2_PRIVATE_KEY not set")

    try:
        from paradex_py import ParadexSubkey
        paradex = ParadexSubkey(env="prod", l2_private_key=l2_key, l2_address=l2_addr)
        jwt = paradex.account.jwt_token
    except ImportError:
        return _snap("Paradex", error="paradex-py not installed")
    except Exception as e:
        return _snap("Paradex", error=f"Auth failed: {e}")

    BASE = "https://api.prod.paradex.trade/v1"
    auth = {"Authorization": f"Bearer {jwt}"}

    async with httpx.AsyncClient(timeout=15) as c:
        # Account
        resp = await c.get(f"{BASE}/account", headers=auth)
        resp.raise_for_status()
        acct = resp.json()
        account_value = float(acct.get("account_value") or 0)
        total_collateral = float(acct.get("total_collateral") or 0)
        free_collateral = float(acct.get("free_collateral") or 0)
        margin_used = total_collateral - free_collateral
        util = (margin_used / total_collateral * 100) if total_collateral > 0 else 0

        # Positions
        resp2 = await c.get(f"{BASE}/positions", headers=auth)
        resp2.raise_for_status()
        all_pos = resp2.json().get("results", [])

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for p in all_pos:
            sz = float(p.get("size") or 0)
            if sz == 0:
                continue
            entry = float(p.get("average_entry_price") or 0)
            u = float(p.get("unrealized_pnl") or 0)
            cost = abs(float(p.get("cost_usd") or 0))
            ntl = cost if cost else abs(sz) * entry
            upnl += u
            total_ntl += ntl
            positions.append({
                "symbol": p.get("market", "?").replace("-USD-PERP", ""),
                "side": p.get("side", "?"),
                "size": abs(sz), "notional": ntl,
                "entry": entry,
                "mark": entry,  # Paradex doesn't return mark in positions
                "upnl": u, "leverage": p.get("leverage", "?"),
            })

        # Orders
        resp3 = await c.get(f"{BASE}/orders", headers=auth)
        ords = resp3.json().get("results", []) if resp3.status_code == 200 else []

        # Fills
        resp4 = await c.get(f"{BASE}/fills", params={"page_size": 20}, headers=auth)
        fills = resp4.json().get("results", []) if resp4.status_code == 200 else []
        total_fees = sum(abs(float(f.get("fee", 0))) for f in fills)

        return _snap("Paradex", equity=account_value, wallet_balance=total_collateral,
                      margin_used=margin_used, margin_free=free_collateral,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions,
                      num_orders=len(ords), num_fills=len(fills),
                      total_fees=total_fees)


async def fetch_ethereal() -> dict:
    address = os.getenv("ETHEREAL_WALLET_ADDRESS", "")
    if not address:
        return _snap("Ethereal", error="ETHEREAL_WALLET_ADDRESS not set")

    BASE = "https://api.ethereal.trade"
    async with httpx.AsyncClient(timeout=15) as c:
        # Resolve subaccount
        resp = await c.get(f"{BASE}/v1/subaccount", params={"sender": address, "limit": 10})
        resp.raise_for_status()
        subs = resp.json().get("data", [])
        if not subs:
            return _snap("Ethereal", error="No subaccounts found")
        sa_id = subs[0]["id"]

        # Balance
        bal_resp = await c.get(f"{BASE}/v1/subaccount/balance",
                               params={"subaccountId": sa_id, "limit": 20})
        bal_resp.raise_for_status()
        balances = bal_resp.json().get("data", [])
        total_balance = sum(float(b.get("amount") or 0) for b in balances)
        total_available = sum(float(b.get("available") or 0) for b in balances)
        margin_used = total_balance - total_available
        util = (margin_used / total_balance * 100) if total_balance > 0 else 0

        # Product map for position symbols
        prod_resp = await c.get(f"{BASE}/v1/product", params={"limit": 100})
        prod_resp.raise_for_status()
        id_to_ticker = {}
        for p in prod_resp.json().get("data", []):
            id_to_ticker[p["id"]] = p.get("displayTicker", "?")

        # Positions
        pos_resp = await c.get(f"{BASE}/v1/position",
                               params={"subaccountId": sa_id, "open": "true", "limit": 50})
        pos_resp.raise_for_status()
        pos_list = pos_resp.json().get("data", [])

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for p in pos_list:
            sz = float(p.get("size") or 0)
            if sz == 0:
                continue
            mark = float(p.get("markPrice") or 0)
            ntl = sz * mark
            u = float(p.get("pnl") or 0)
            upnl += u
            total_ntl += ntl
            positions.append({
                "symbol": id_to_ticker.get(p.get("productId", ""), "?"),
                "side": "LONG" if p.get("side", 0) == 0 else "SHORT",
                "size": sz, "notional": ntl,
                "entry": float(p.get("entryPrice") or 0),
                "mark": mark, "upnl": u,
                "leverage": p.get("leverage", "?"),
            })

        # Orders
        ord_resp = await c.get(f"{BASE}/v1/order",
                               params={"subaccountId": sa_id, "isWorking": "true", "limit": 50})
        ords = ord_resp.json().get("data", []) if ord_resp.status_code == 200 else []

        return _snap("Ethereal", equity=total_balance, wallet_balance=total_balance - upnl,
                      margin_used=margin_used, margin_free=total_available,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions,
                      num_orders=len(ords))


async def fetch_tradexyz() -> dict:
    address = os.getenv("TRADEXYZ_WALLET_ADDRESS", os.getenv("HYPERLIQUID_WALLET_ADDRESS", ""))
    if not address:
        return _snap("trade.xyz", error="TRADEXYZ/HYPERLIQUID_WALLET_ADDRESS not set")

    API = "https://api.hyperliquid.xyz/info"
    async with httpx.AsyncClient(timeout=15) as c:
        raw = (await c.post(API, json={
            "type": "clearinghouseState", "user": address, "dex": "xyz",
        })).json()

        cross = raw.get("crossMarginSummary", {})
        equity = float(cross.get("accountValue", 0))
        wallet = float(cross.get("totalRawUsd", 0))
        margin_used = float(cross.get("totalMarginUsed", 0))
        margin_free = equity - margin_used
        util = (margin_used / equity * 100) if equity else 0

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for item in raw.get("assetPositions", []):
            p = item.get("position", item)
            sz = float(p.get("szi", 0))
            if sz == 0:
                continue
            ntl = abs(float(p.get("positionValue", 0)))
            u = float(p.get("unrealizedPnl", 0))
            upnl += u
            total_ntl += ntl
            lev = p.get("leverage", {})
            lev_val = lev.get("value", "?") if isinstance(lev, dict) else lev
            coin = p["coin"]
            if coin.startswith("xyz:"):
                coin = coin[4:]
            elif ":" in coin:
                coin = coin.split(":", 1)[1]
            positions.append({
                "symbol": coin, "side": "LONG" if sz > 0 else "SHORT",
                "size": abs(sz), "notional": ntl,
                "entry": float(p.get("entryPx", 0)),
                "mark": ntl / abs(sz) if sz else 0,
                "upnl": u, "leverage": lev_val,
            })

        # Orders
        ords = (await c.post(API, json={
            "type": "openOrders", "user": address, "dex": "xyz",
        })).json()

        return _snap("trade.xyz", equity=equity, wallet_balance=wallet,
                      margin_used=margin_used, margin_free=margin_free,
                      margin_util_pct=util, unrealized_pnl=upnl,
                      position_notional=total_ntl, positions=positions,
                      num_orders=len(ords) if isinstance(ords, list) else 0)


async def fetch_extended() -> dict:
    api_key = os.getenv("EXTENDED_API_KEY", "")
    if not api_key:
        return _snap("Extended", error="EXTENDED_API_KEY not set")

    BASE = "https://api.starknet.extended.exchange/api/v1"
    headers = {"User-Agent": "hedgehog/1.0", "X-Api-Key": api_key}

    def unwrap(body):
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    async with httpx.AsyncClient(timeout=15) as c:
        # Balance
        resp = await c.get(f"{BASE}/user/balance", headers=headers)
        resp.raise_for_status()
        bal = unwrap(resp.json())
        if isinstance(bal, list):
            bal = bal[0] if bal else {}

        equity = float(bal.get("equity", 0) or 0)
        available = float(bal.get("availableForTrade", bal.get("available", 0)) or 0)
        upnl_total = float(bal.get("unrealisedPnl", bal.get("unrealizedPnl", 0)) or 0)
        margin = float(bal.get("initialMargin", 0) or 0)
        balance = float(bal.get("balance", 0) or 0)
        util = (margin / equity * 100) if equity > 0 else 0

        # Positions
        resp2 = await c.get(f"{BASE}/user/positions", headers=headers)
        resp2.raise_for_status()
        pos_data = unwrap(resp2.json())
        if not isinstance(pos_data, list):
            pos_data = pos_data.get("positions", []) if isinstance(pos_data, dict) else []

        positions = []
        upnl = 0.0
        total_ntl = 0.0
        for p in pos_data:
            sz = float(p.get("size", 0) or 0)
            if abs(sz) < 1e-12:
                continue
            value = abs(float(p.get("value", 0) or 0))
            u = float(p.get("unrealisedPnl", p.get("unrealizedPnl", 0)) or 0)
            upnl += u
            total_ntl += value
            positions.append({
                "symbol": p.get("market", "?").replace("-USD", "").replace("-PERP", ""),
                "side": p.get("side", "?").upper(),
                "size": abs(sz), "notional": value,
                "entry": float(p.get("openPrice", p.get("entryPrice", 0)) or 0),
                "mark": float(p.get("markPrice", 0) or 0),
                "upnl": u, "leverage": "?",
            })

        # Orders
        resp3 = await c.get(f"{BASE}/user/orders", headers=headers)
        ords = unwrap(resp3.json()) if resp3.status_code == 200 else []
        if not isinstance(ords, list):
            ords = ords.get("orders", []) if isinstance(ords, dict) else []

        return _snap("Extended", equity=equity, wallet_balance=balance,
                      margin_used=margin, margin_free=available,
                      margin_util_pct=util, unrealized_pnl=upnl_total,
                      position_notional=total_ntl, positions=positions,
                      num_orders=len(ords))


# ═════════════════════════════════════════════════════════════════════════
# DISPLAY
# ═════════════════════════════════════════════════════════════════════════

def _color_pnl(val: float) -> str:
    if val > 0:
        return f"{G}${val:>+12,.2f}{X}"
    elif val < 0:
        return f"{R}${val:>+12,.2f}{X}"
    return f"${val:>+12,.2f}"


def print_venue(snap: dict, brief: bool = False):
    venue = snap["venue"]
    print(f"\n{'─' * 72}")
    if snap["error"]:
        err_msg = snap["error"].split("\n")[0][:80]
        print(f"  {B}{venue}{X}  {Y}[SKIP]{X}  {err_msg}")
        return

    eq = snap["equity"]
    print(f"  {B}{venue}{X}")
    print(f"  Equity:        ${eq:>12,.2f}    Wallet Bal:    ${snap['wallet_balance']:>12,.2f}")
    print(f"  Margin Used:   ${snap['margin_used']:>12,.2f}    Free Margin:   ${snap['margin_free']:>12,.2f}")
    print(f"  Margin Util:   {snap['margin_util_pct']:>11.1f}%    uPnL:          {_color_pnl(snap['unrealized_pnl'])}")

    n_pos = len(snap["positions"])
    n_ord = snap["num_orders"]
    n_fill = snap["num_fills"]
    fees = snap["total_fees"]
    summary_parts = [f"{n_pos} positions"]
    if n_ord:
        summary_parts.append(f"{n_ord} orders")
    if n_fill:
        summary_parts.append(f"{n_fill} fills")
    if fees > 0:
        summary_parts.append(f"${fees:,.2f} fees")
    if snap["position_notional"] > 0:
        summary_parts.append(f"${snap['position_notional']:,.0f} notional")
    print(f"  Activity:      {', '.join(summary_parts)}")

    if not brief and snap["positions"]:
        print(f"\n  {'SYM':>8s} {'SIDE':>5s} {'SIZE':>10s} {'NOTIONAL':>12s} "
              f"{'ENTRY':>10s} {'MARK':>10s} {'uPnL':>12s} {'LEV':>5s}")
        print("  " + "-" * 78)
        for p in sorted(snap["positions"], key=lambda x: x["notional"], reverse=True):
            u = p["upnl"]
            uc = G if u > 0 else R if u < 0 else ""
            rx = X if uc else ""
            print(f"  {p['symbol']:>8s} {p['side']:>5s} {p['size']:>10.4f} "
                  f"${p['notional']:>11,.2f} ${p['entry']:>9,.2f} ${p['mark']:>9,.2f} "
                  f"{uc}${u:>+11,.2f}{rx} {str(p['leverage']):>5s}x")


def print_totals(snaps: list[dict]):
    ok = [s for s in snaps if not s["error"]]
    err = [s for s in snaps if s["error"]]

    total_equity = sum(s["equity"] for s in ok)
    total_wallet = sum(s["wallet_balance"] for s in ok)
    total_margin_used = sum(s["margin_used"] for s in ok)
    total_margin_free = sum(s["margin_free"] for s in ok)
    total_upnl = sum(s["unrealized_pnl"] for s in ok)
    total_notional = sum(s["position_notional"] for s in ok)
    total_positions = sum(len(s["positions"]) for s in ok)
    total_orders = sum(s["num_orders"] for s in ok)
    total_fills = sum(s["num_fills"] for s in ok)
    total_fees = sum(s["total_fees"] for s in ok)
    avg_util = (total_margin_used / total_equity * 100) if total_equity > 0 else 0

    print(f"\n{'═' * 72}")
    print(f"  {B}SYSTEM TOTALS{X}  ({len(ok)}/{len(snaps)} venues connected)")
    print(f"{'═' * 72}")
    print(f"  Total Equity:            ${total_equity:>14,.2f}")
    print(f"  Total Wallet Balance:    ${total_wallet:>14,.2f}")
    print(f"  Total Margin Used:       ${total_margin_used:>14,.2f}")
    print(f"  Total Margin Free:       ${total_margin_free:>14,.2f}")
    print(f"  Avg Margin Utilization:  {avg_util:>13.1f}%")
    print(f"  Total uPnL:              {_color_pnl(total_upnl)}")
    print(f"  Total Position Notional: ${total_notional:>14,.0f}")
    print(f"  Total Open Positions:    {total_positions:>14d}")
    print(f"  Total Open Orders:       {total_orders:>14d}")
    if total_fills:
        print(f"  Recent Fills:            {total_fills:>14d}")
    if total_fees > 0:
        print(f"  Recent Fees:             ${total_fees:>14,.2f}")

    if err:
        print(f"\n  {Y}Skipped venues:{X}")
        for s in err:
            print(f"    {s['venue']}: {s['error'].split(chr(10))[0][:80]}")

    # Per-venue equity breakdown
    if ok:
        print(f"\n  {'VENUE':<14s} {'EQUITY':>12s} {'MARGIN USED':>12s} "
              f"{'UTIL%':>7s} {'uPnL':>12s} {'POSITIONS':>10s}")
        print("  " + "-" * 69)
        for s in sorted(ok, key=lambda x: x["equity"], reverse=True):
            u = s["unrealized_pnl"]
            uc = G if u > 0 else R if u < 0 else ""
            rx = X if uc else ""
            print(f"  {s['venue']:<14s} ${s['equity']:>11,.2f} ${s['margin_used']:>11,.2f} "
                  f"{s['margin_util_pct']:>6.1f}% "
                  f"{uc}${u:>+11,.2f}{rx} {len(s['positions']):>10d}")

    print(f"{'═' * 72}")


# ═════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════

ALL_FETCHERS = [
    fetch_hyperliquid,
    fetch_aster,
    fetch_lighter,
    fetch_apex,
    fetch_dydx,
    fetch_drift,
    fetch_edgex,
    fetch_paradex,
    fetch_ethereal,
    fetch_tradexyz,
    fetch_extended,
]


async def _safe_fetch(fn):
    try:
        return await fn()
    except Exception as e:
        venue = fn.__name__.replace("fetch_", "").replace("_", " ").title()
        return _snap(venue, error=str(e))


async def main():
    brief = "--brief" in sys.argv

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"{'═' * 72}")
    print(f"  {B}HEDGEHOG ACCOUNT SNAPSHOT{X} — {now}")
    print(f"{'═' * 72}")
    print(f"  Querying {len(ALL_FETCHERS)} venues in parallel...")

    snaps = await asyncio.gather(*[_safe_fetch(fn) for fn in ALL_FETCHERS])

    for snap in snaps:
        print_venue(snap, brief=brief)

    print_totals(snaps)


if __name__ == "__main__":
    asyncio.run(main())
