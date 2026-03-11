"""Venue portfolio fetchers — structured JSON from each exchange API."""
import hashlib
import hmac
import os
import time
from collections import defaultdict

import httpx

# ── Helpers ──────────────────────────────────────────────────


def _env(key: str) -> str:
    return os.environ.get(key, "")


def normalize_symbol(raw: str) -> str:
    """Strip common suffixes to get base symbol."""
    s = raw.upper().strip()
    for suffix in ("-USD-PERP", "-PERP", "-USD", "USDT", "USDC", "-USDT", "-USDC"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def _err(venue: str, e: Exception) -> dict:
    return {"venue": venue, "status": "error", "error": str(e), "balance": {}, "positions": []}


def _skip(venue: str, reason: str = "no credentials") -> dict:
    return {"venue": venue, "status": "skip", "error": reason, "balance": {}, "positions": []}


# ═══════════════════════════════════════════════════════════════
# 1. HYPERLIQUID — no auth for reads
# ═══════════════════════════════════════════════════════════════

async def fetch_hl(client: httpx.AsyncClient) -> dict:
    address = _env("HYPERLIQUID_WALLET_ADDRESS")
    if not address:
        return _skip("HL")
    try:
        resp = await client.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": address},
        )
        resp.raise_for_status()
        raw = resp.json()

        cross = raw.get("crossMarginSummary", {})
        equity = float(cross.get("accountValue", 0))
        margin_used = float(cross.get("totalMarginUsed", 0))
        margin_free = equity - margin_used
        margin_util = (margin_used / equity * 100) if equity else 0

        positions = []
        upnl = 0.0
        for item in raw.get("assetPositions", []):
            p = item.get("position", item)
            sz = float(p.get("szi", 0))
            if sz == 0:
                continue
            pos_upnl = float(p.get("unrealizedPnl", 0))
            upnl += pos_upnl
            notional = abs(float(p.get("positionValue", 0)))
            lev = p.get("leverage", {})
            lev_val = float(lev.get("value", 0)) if isinstance(lev, dict) else float(lev or 0)
            positions.append({
                "venue": "HL",
                "symbol": normalize_symbol(p.get("coin", "")),
                "raw_symbol": p.get("coin", ""),
                "side": "LONG" if sz > 0 else "SHORT",
                "size": abs(sz),
                "notional": notional,
                "entry_price": float(p.get("entryPx", 0)),
                "mark_price": notional / abs(sz) if sz else 0,
                "unrealized_pnl": pos_upnl,
                "leverage": lev_val,
                "liquidation_price": float(p.get("liquidationPx", 0) or 0),
            })

        return {
            "venue": "HL",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": float(cross.get("totalRawUsd", 0)),
                "margin_used": margin_used,
                "margin_free": margin_free,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": float(raw.get("withdrawable", 0)),
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("HL", e)


# ═══════════════════════════════════════════════════════════════
# 2. TRADE.XYZ — same HL API with dex="xyz"
# ═══════════════════════════════════════════════════════════════

async def fetch_tradexyz(client: httpx.AsyncClient) -> dict:
    address = _env("TRADEXYZ_WALLET_ADDRESS") or _env("HYPERLIQUID_WALLET_ADDRESS")
    if not address:
        return _skip("XYZ")
    try:
        resp = await client.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": address, "dex": "xyz"},
        )
        resp.raise_for_status()
        raw = resp.json()

        cross = raw.get("crossMarginSummary", {})
        equity = float(cross.get("accountValue", 0))
        margin_used = float(cross.get("totalMarginUsed", 0))
        margin_free = equity - margin_used
        margin_util = (margin_used / equity * 100) if equity else 0

        positions = []
        upnl = 0.0
        for item in raw.get("assetPositions", []):
            p = item.get("position", item)
            sz = float(p.get("szi", 0))
            if sz == 0:
                continue
            pos_upnl = float(p.get("unrealizedPnl", 0))
            upnl += pos_upnl
            notional = abs(float(p.get("positionValue", 0)))
            lev = p.get("leverage", {})
            lev_val = float(lev.get("value", 0)) if isinstance(lev, dict) else float(lev or 0)
            coin = p.get("coin", "")
            positions.append({
                "venue": "XYZ",
                "symbol": normalize_symbol(coin.replace("xyz:", "")),
                "raw_symbol": coin,
                "side": "LONG" if sz > 0 else "SHORT",
                "size": abs(sz),
                "notional": notional,
                "entry_price": float(p.get("entryPx", 0)),
                "mark_price": notional / abs(sz) if sz else 0,
                "unrealized_pnl": pos_upnl,
                "leverage": lev_val,
                "liquidation_price": float(p.get("liquidationPx", 0) or 0),
            })

        return {
            "venue": "XYZ",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": float(cross.get("totalRawUsd", 0)),
                "margin_used": margin_used,
                "margin_free": margin_free,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": float(raw.get("withdrawable", 0)),
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("XYZ", e)


# ═══════════════════════════════════════════════════════════════
# 3. DYDX V4 — no auth for reads
# ═══════════════════════════════════════════════════════════════

async def fetch_dydx(client: httpx.AsyncClient) -> dict:
    address = _env("DYDX_WALLET_ADDRESS")
    if not address:
        return _skip("dYdX")
    try:
        resp = await client.get(f"https://indexer.dydx.trade/v4/addresses/{address}")
        resp.raise_for_status()
        data = resp.json()

        subs = data.get("subaccounts", [])
        if not subs:
            return {"venue": "dYdX", "status": "ok", "error": None,
                    "balance": {"equity": 0, "wallet_balance": 0, "margin_used": 0,
                                "margin_free": 0, "margin_util_pct": 0,
                                "unrealized_pnl": 0, "withdrawable": 0},
                    "positions": []}

        sub = subs[0]
        equity = float(sub.get("equity", 0))
        free = float(sub.get("freeCollateral", 0))
        margin_used = equity - free
        margin_util = (margin_used / equity * 100) if equity else 0

        # Wallet balance from USDC asset position (assetPositions is a dict)
        wallet = 0.0
        asset_positions = sub.get("assetPositions", {})
        if isinstance(asset_positions, dict):
            usdc = asset_positions.get("USDC", {})
            if usdc:
                sz = float(usdc.get("size", 0))
                wallet = sz if usdc.get("side") == "LONG" else -sz
        elif isinstance(asset_positions, list):
            for ap in asset_positions:
                if ap.get("symbol") == "USDC":
                    sz = float(ap.get("size", 0))
                    wallet = sz if ap.get("side") == "LONG" else -sz

        positions = []
        upnl = 0.0
        perps = sub.get("openPerpetualPositions", {})
        for market, pp in perps.items():
            sz = float(pp.get("size", 0))
            if sz == 0:
                continue
            pos_upnl = float(pp.get("unrealizedPnl", 0))
            upnl += pos_upnl
            entry = float(pp.get("entryPrice", 0))
            # Mark price from oracle prices or entryPrice as fallback
            mark = entry  # will be enriched if we add oracle price fetch
            positions.append({
                "venue": "dYdX",
                "symbol": normalize_symbol(market),
                "raw_symbol": market,
                "side": pp.get("side", "LONG" if sz > 0 else "SHORT"),
                "size": abs(sz),
                "notional": abs(sz) * entry,
                "entry_price": entry,
                "mark_price": mark,
                "unrealized_pnl": pos_upnl,
                "leverage": None,
                "liquidation_price": None,
            })

        return {
            "venue": "dYdX",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": wallet,
                "margin_used": margin_used,
                "margin_free": free,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": free,
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("dYdX", e)


# ═══════════════════════════════════════════════════════════════
# 4. DRIFT — no auth for reads
# ═══════════════════════════════════════════════════════════════

async def fetch_drift(client: httpx.AsyncClient) -> dict:
    address = _env("DRIFT_WALLET_ADDRESS")
    if not address:
        return _skip("Drift")
    try:
        # Resolve authority to account
        resp = await client.get(f"https://data.api.drift.trade/authority/{address}/accounts")
        resp.raise_for_status()
        acct_resp = resp.json()
        # Response format: {"success": true, "accounts": [...]}
        accounts = acct_resp.get("accounts", acct_resp) if isinstance(acct_resp, dict) else acct_resp
        if isinstance(accounts, dict):
            accounts = accounts.get("accounts", [])
        if not accounts:
            return _skip("Drift", "no accounts found")

        account_id = accounts[0].get("accountId") or accounts[0].get("account_id") or accounts[0].get("id", "")

        resp2 = await client.get(f"https://data.api.drift.trade/user/{account_id}")
        resp2.raise_for_status()
        data = resp2.json()

        acct = data.get("account", data)
        equity = float(acct.get("totalCollateral", 0))
        free = float(acct.get("freeCollateral", 0))
        margin_used = equity - free
        margin_util = (margin_used / equity * 100) if equity else 0

        positions = []
        upnl = 0.0
        for pos in data.get("positions", []):
            base = float(pos.get("baseAssetAmount", 0))
            if base == 0:
                continue
            pos_upnl = float(pos.get("unrealizedPnl", 0))
            upnl += pos_upnl
            entry = float(pos.get("entryPrice", 0))
            mark = float(pos.get("markPrice", 0)) or entry
            sym = pos.get("symbol", "")
            positions.append({
                "venue": "Drift",
                "symbol": normalize_symbol(sym),
                "raw_symbol": sym,
                "side": "LONG" if base > 0 else "SHORT",
                "size": abs(base),
                "notional": abs(base) * mark,
                "entry_price": entry,
                "mark_price": mark,
                "unrealized_pnl": pos_upnl,
                "leverage": float(pos.get("leverage", 0)) or None,
                "liquidation_price": float(pos.get("liquidationPrice", 0)) or None,
            })

        return {
            "venue": "Drift",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": float(acct.get("balance", 0)),
                "margin_used": margin_used,
                "margin_free": free,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": free,
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("Drift", e)


# ═══════════════════════════════════════════════════════════════
# 5. LIGHTER — no auth for account reads
# ═══════════════════════════════════════════════════════════════

async def fetch_lighter(client: httpx.AsyncClient) -> dict:
    idx = _env("LIGHTER_ACCOUNT_INDEX")
    if not idx:
        return _skip("Lighter")
    try:
        resp = await client.get(
            "https://mainnet.zklighter.elliot.ai/api/v1/account",
            params={"by": "index", "value": idx},
        )
        resp.raise_for_status()
        data = resp.json()
        accounts = data.get("accounts", [])
        if not accounts:
            return _skip("Lighter", "no account data")

        acct = accounts[0]
        collateral = float(acct.get("collateral", 0))
        available = float(acct.get("available_balance", 0))
        nav = float(acct.get("total_asset_value", 0)) or collateral
        margin_used = collateral - available
        margin_util = (margin_used / collateral * 100) if collateral else 0

        positions = []
        upnl = 0.0
        for pos in acct.get("positions", []):
            size_val = float(pos.get("position", 0))
            if size_val == 0:
                continue
            pos_upnl = float(pos.get("unrealized_pnl", 0))
            upnl += pos_upnl
            entry = float(pos.get("avg_entry_price", 0))
            pos_value = float(pos.get("position_value", 0))
            mark = abs(pos_value) / abs(size_val) if size_val else entry
            imf = float(pos.get("initial_margin_fraction", 0))
            lev = 100.0 / imf if imf > 0 else None
            liq = float(pos.get("liquidation_price", 0) or 0)
            positions.append({
                "venue": "Lighter",
                "symbol": normalize_symbol(pos.get("symbol", "")),
                "raw_symbol": pos.get("symbol", ""),
                "side": "LONG" if size_val > 0 else "SHORT",
                "size": abs(size_val),
                "notional": abs(pos_value),
                "entry_price": entry,
                "mark_price": mark,
                "unrealized_pnl": pos_upnl,
                "leverage": lev,
                "liquidation_price": liq if liq > 0 else None,
            })

        return {
            "venue": "Lighter",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": nav,
                "wallet_balance": collateral,
                "margin_used": margin_used,
                "margin_free": available,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": available,
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("Lighter", e)


# ═══════════════════════════════════════════════════════════════
# 6. ETHEREAL — address-based reads
# ═══════════════════════════════════════════════════════════════

async def fetch_ethereal(client: httpx.AsyncClient) -> dict:
    address = _env("ETHEREAL_WALLET_ADDRESS")
    if not address:
        return _skip("Ethereal")
    try:
        # Get subaccount
        resp = await client.get(
            "https://api.ethereal.trade/v1/subaccount",
            params={"sender": address},
        )
        resp.raise_for_status()
        sub_data = resp.json()
        subs = sub_data.get("data", [])
        if not subs:
            return _skip("Ethereal", "no subaccounts")

        sub_id = subs[0].get("subaccountId") or subs[0].get("id")

        # Get balance
        bal_resp = await client.get(
            "https://api.ethereal.trade/v1/subaccount/balance",
            params={"subaccountId": sub_id},
        )
        bal_resp.raise_for_status()
        bal_data = bal_resp.json()

        total_bal = 0.0
        total_avail = 0.0
        for b in bal_data.get("data", []):
            total_bal += float(b.get("amount", 0))
            total_avail += float(b.get("available", 0))

        margin_used = total_bal - total_avail
        margin_util = (margin_used / total_bal * 100) if total_bal else 0

        # Get positions
        pos_resp = await client.get(
            "https://api.ethereal.trade/v1/position",
            params={"subaccountId": sub_id, "open": "true"},
        )
        pos_resp.raise_for_status()
        pos_data = pos_resp.json()

        positions = []
        upnl = 0.0
        for p in pos_data.get("data", []):
            sz = float(p.get("size", 0))
            if sz == 0:
                continue
            pos_upnl = float(p.get("pnl", 0))
            upnl += pos_upnl
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0)) or entry
            side_val = p.get("side", 0)
            side = "SHORT" if side_val == 1 else "LONG"
            liq = float(p.get("liquidationPrice", 0) or 0)
            positions.append({
                "venue": "Ethereal",
                "symbol": normalize_symbol(str(p.get("symbol", p.get("productId", "")))),
                "raw_symbol": str(p.get("symbol", "")),
                "side": side,
                "size": abs(sz),
                "notional": abs(sz) * mark,
                "entry_price": entry,
                "mark_price": mark,
                "unrealized_pnl": pos_upnl,
                "leverage": float(p.get("leverage", 0)) or None,
                "liquidation_price": liq if liq > 0 else None,
            })

        return {
            "venue": "Ethereal",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": total_bal,
                "wallet_balance": total_bal,
                "margin_used": margin_used,
                "margin_free": total_avail,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": total_avail,
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("Ethereal", e)


# ═══════════════════════════════════════════════════════════════
# 7. ASTER — HMAC-SHA256 auth
# ═══════════════════════════════════════════════════════════════

def _aster_sign(params: dict, secret: str) -> dict:
    ts = str(int(time.time() * 1000))
    params["timestamp"] = ts
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


async def fetch_aster(client: httpx.AsyncClient) -> dict:
    api_key = _env("ASTER_API_KEY")
    api_secret = _env("ASTER_API_SECRET")
    if not api_key or not api_secret:
        return _skip("Aster")
    try:
        params = _aster_sign({}, api_secret)
        resp = await client.get(
            "https://fapi.asterdex.com/fapi/v4/account",
            params=params,
            headers={"X-MBX-APIKEY": api_key},
        )
        resp.raise_for_status()
        data = resp.json()

        equity = float(data.get("totalMarginBalance", 0))
        wallet = float(data.get("totalWalletBalance", 0))
        margin_used = float(data.get("totalPositionInitialMargin", 0)) + float(data.get("totalOpenOrderInitialMargin", 0))
        margin_free = float(data.get("availableBalance", 0))
        margin_util = (margin_used / equity * 100) if equity else 0
        total_upnl = float(data.get("totalUnrealizedProfit", 0))

        positions = []
        for p in data.get("positions", []):
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            pos_upnl = float(p.get("unrealizedProfit", 0))
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0)) or entry
            notional = abs(float(p.get("notional", 0))) or abs(amt) * mark
            liq = float(p.get("liquidationPrice", 0) or 0)
            positions.append({
                "venue": "Aster",
                "symbol": normalize_symbol(p.get("symbol", "")),
                "raw_symbol": p.get("symbol", ""),
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "notional": notional,
                "entry_price": entry,
                "mark_price": mark,
                "unrealized_pnl": pos_upnl,
                "leverage": float(p.get("leverage", 0)) or None,
                "liquidation_price": liq if liq > 0 else None,
            })

        return {
            "venue": "Aster",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": wallet,
                "margin_used": margin_used,
                "margin_free": margin_free,
                "margin_util_pct": margin_util,
                "unrealized_pnl": total_upnl,
                "withdrawable": margin_free,
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("Aster", e)


# ═══════════════════════════════════════════════════════════════
# 8. APEX OMNI — HMAC auth
# ═══════════════════════════════════════════════════════════════

def _apex_sign(method: str, path: str, qs: str, ts: str, secret: str) -> str:
    msg = ts + method + path
    if qs:
        msg += "?" + qs
    sig = hmac.new(
        secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    import base64
    return base64.b64encode(bytes.fromhex(sig)).decode()


async def fetch_apex(client: httpx.AsyncClient) -> dict:
    api_key = _env("APEX_OMNI_API_KEY")
    api_secret = _env("APEX_OMNI_API_SECRET")
    passphrase = _env("APEX_OMNI_PASSPHRASE")
    if not api_key or not api_secret:
        return _skip("Apex")
    try:
        ts = str(int(time.time() * 1000))
        path = "/api/v3/account-balance"
        sig = _apex_sign("GET", path, "", ts, api_secret)
        headers = {
            "APEX-SIGNATURE": sig,
            "APEX-API-KEY": api_key,
            "APEX-TIMESTAMP": ts,
            "APEX-PASSPHRASE": passphrase or "",
        }
        resp = await client.get(
            f"https://omni.apex.exchange{path}",
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json()
        data = result.get("data", result)

        equity = float(data.get("totalEquityValue", 0))
        available = float(data.get("availableBalance", 0))
        margin_used = float(data.get("initialMargin", 0))
        margin_util = (margin_used / equity * 100) if equity else 0
        total_upnl = float(data.get("unrealizedPnl", 0))

        positions = []
        # Try positions from account-balance or fall back to /api/v3/account
        for p in data.get("positions", data.get("openPositions", {}).values() if isinstance(data.get("openPositions"), dict) else []):
            if isinstance(p, str):
                continue
            sz = float(p.get("size", 0))
            if sz == 0:
                continue
            pos_upnl = float(p.get("unrealizedPnl", 0))
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0)) or entry
            side = p.get("side", "LONG" if sz > 0 else "SHORT")
            positions.append({
                "venue": "Apex",
                "symbol": normalize_symbol(p.get("symbol", "")),
                "raw_symbol": p.get("symbol", ""),
                "side": side.upper(),
                "size": abs(sz),
                "notional": abs(sz) * mark,
                "entry_price": entry,
                "mark_price": mark,
                "unrealized_pnl": pos_upnl,
                "leverage": float(p.get("leverage", 0)) or None,
                "liquidation_price": float(p.get("liquidationPrice", 0) or 0) or None,
            })

        return {
            "venue": "Apex",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": equity,
                "margin_used": margin_used,
                "margin_free": available,
                "margin_util_pct": margin_util,
                "unrealized_pnl": total_upnl,
                "withdrawable": available,
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("Apex", e)


# ═══════════════════════════════════════════════════════════════
# 9. EDGEX — ECDSA/SHA3 auth
# ═══════════════════════════════════════════════════════════════

async def fetch_edgex(client: httpx.AsyncClient) -> dict:
    account_id = _env("EDGEX_ACCOUNT_ID")
    private_key = _env("EDGEX_PRIVATE_KEY")
    if not account_id or not private_key:
        return _skip("EdgeX")
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        import hashlib as _hl

        ts = str(int(time.time() * 1000))
        path = f"/api/v1/private/account/getAccountAsset?accountId={account_id}"
        msg_hash = _hl.sha3_256(f"{ts}GET{path}".encode()).hexdigest()
        acct = Account.from_key(private_key)
        signed = acct.sign_message(encode_defunct(hexstr=msg_hash))
        sig = signed.signature.hex()

        resp = await client.get(
            f"https://pro.edgex.exchange{path}",
            headers={
                "X-edgeX-Api-Timestamp": ts,
                "X-edgeX-Api-Signature": f"0x{sig}",
            },
        )
        resp.raise_for_status()
        result = resp.json()
        data = result.get("data", result)

        equity = float(data.get("totalEquityValue", 0))
        available = float(data.get("availableAmount", 0))
        margin_used = equity - available
        margin_util = (margin_used / equity * 100) if equity else 0

        positions = []
        upnl = 0.0
        for p in data.get("positionAssetList", []):
            sz = float(p.get("openSize", 0))
            if sz == 0:
                continue
            pos_upnl = float(p.get("unrealizePnl", 0))
            upnl += pos_upnl
            open_val = abs(float(p.get("openValue", 0)))
            entry = open_val / abs(sz) if sz else 0
            mark = float(p.get("markPrice", 0)) or entry
            positions.append({
                "venue": "EdgeX",
                "symbol": normalize_symbol(str(p.get("contractId", ""))),
                "raw_symbol": str(p.get("contractId", "")),
                "side": "LONG" if sz > 0 else "SHORT",
                "size": abs(sz),
                "notional": open_val,
                "entry_price": entry,
                "mark_price": mark,
                "unrealized_pnl": pos_upnl,
                "leverage": None,
                "liquidation_price": None,
            })

        return {
            "venue": "EdgeX",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": equity,
                "margin_used": margin_used,
                "margin_free": available,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": available,
            },
            "positions": positions,
        }
    except Exception as e:
        return _err("EdgeX", e)


# ═══════════════════════════════════════════════════════════════
# 10. PARADEX — StarkNet JWT auth
# ═══════════════════════════════════════════════════════════════

async def fetch_paradex(client: httpx.AsyncClient) -> dict:
    l2_addr = _env("PARADEX_L2_ADDRESS")
    l2_key = _env("PARADEX_L2_PRIVATE_KEY")
    if not l2_addr or not l2_key:
        return _skip("Paradex")
    try:
        from paradex_py import Paradex
        from paradex_py.environment import Environment

        paradex = Paradex(env=Environment.PROD, l2_address=l2_addr, l2_private_key=l2_key)
        headers = {"Authorization": f"Bearer {paradex.jwt_token}"}

        # Account
        resp = await client.get(
            "https://api.prod.paradex.trade/v1/account",
            headers=headers,
        )
        resp.raise_for_status()
        acct = resp.json()

        equity = float(acct.get("account_value", 0))
        collateral = float(acct.get("total_collateral", 0))
        free = float(acct.get("free_collateral", 0))
        margin_used = collateral - free
        margin_util = (margin_used / collateral * 100) if collateral else 0

        # Positions
        pos_resp = await client.get(
            "https://api.prod.paradex.trade/v1/positions",
            headers=headers,
        )
        pos_resp.raise_for_status()
        pos_data = pos_resp.json()

        positions = []
        upnl = 0.0
        for p in pos_data.get("results", []):
            sz = float(p.get("size", 0))
            if sz == 0:
                continue
            pos_upnl = float(p.get("unrealized_pnl", 0))
            upnl += pos_upnl
            entry = float(p.get("average_entry_price", 0))
            side = p.get("side", "LONG" if sz > 0 else "SHORT")
            positions.append({
                "venue": "Paradex",
                "symbol": normalize_symbol(p.get("market", "")),
                "raw_symbol": p.get("market", ""),
                "side": side.upper(),
                "size": abs(sz),
                "notional": abs(sz) * entry,
                "entry_price": entry,
                "mark_price": entry,  # Paradex doesn't always return mark
                "unrealized_pnl": pos_upnl,
                "leverage": float(p.get("leverage", 0)) or None,
                "liquidation_price": float(p.get("liquidation_price", 0) or 0) or None,
            })

        return {
            "venue": "Paradex",
            "status": "ok",
            "error": None,
            "balance": {
                "equity": equity,
                "wallet_balance": collateral,
                "margin_used": margin_used,
                "margin_free": free,
                "margin_util_pct": margin_util,
                "unrealized_pnl": upnl,
                "withdrawable": free,
            },
            "positions": positions,
        }
    except ImportError:
        return _skip("Paradex", "paradex-py not installed")
    except Exception as e:
        return _err("Paradex", e)


# ═══════════════════════════════════════════════════════════════
# AGGREGATOR
# ═══════════════════════════════════════════════════════════════

ALL_FETCHERS = [
    fetch_hl, fetch_tradexyz, fetch_dydx, fetch_drift,
    fetch_lighter, fetch_ethereal, fetch_aster, fetch_apex,
    fetch_edgex, fetch_paradex,
]


async def fetch_all_portfolios() -> list[dict]:
    """Fetch portfolio data from all venues in parallel."""
    import asyncio

    async with httpx.AsyncClient(timeout=20.0) as client:
        results = await asyncio.gather(
            *[fn(client) for fn in ALL_FETCHERS],
            return_exceptions=True,
        )

    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            venue_name = ALL_FETCHERS[i].__name__.replace("fetch_", "").upper()
            out.append(_err(venue_name, r))
        else:
            out.append(r)
    return out


def build_hedge_groups(venues: list[dict]) -> list[dict]:
    """Group positions by normalized symbol into hedge pairs."""
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for v in venues:
        for pos in v.get("positions", []):
            by_symbol[pos["symbol"]].append(pos)

    groups = []
    for symbol, legs in sorted(by_symbol.items()):
        has_long = any(l["side"] == "LONG" for l in legs)
        has_short = any(l["side"] == "SHORT" for l in legs)
        total_notional = sum(l["notional"] for l in legs)
        net_notional = sum(
            l["notional"] * (1 if l["side"] == "LONG" else -1) for l in legs
        )
        net_upnl = sum(l["unrealized_pnl"] for l in legs)

        if total_notional < 10:
            status = "dust"
        elif has_long and has_short:
            imbalance = abs(net_notional) / total_notional if total_notional else 1
            status = "hedged" if imbalance < 0.15 else "imbalanced"
        else:
            status = "unhedged"

        # Collect venue names per side
        short_venues = [l["venue"] for l in legs if l["side"] == "SHORT"]
        long_venues = [l["venue"] for l in legs if l["side"] == "LONG"]

        groups.append({
            "symbol": symbol,
            "legs": legs,
            "short_venues": list(set(short_venues)),
            "long_venues": list(set(long_venues)),
            "total_notional": total_notional,
            "net_notional": net_notional,
            "net_upnl": net_upnl,
            "status": status,
            "leg_count": len(legs),
        })

    # Sort: hedged first, then by notional descending
    status_order = {"hedged": 0, "imbalanced": 1, "unhedged": 2, "dust": 3}
    groups.sort(key=lambda g: (status_order.get(g["status"], 9), -g["total_notional"]))
    return groups
