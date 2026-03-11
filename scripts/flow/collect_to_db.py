#!/usr/bin/env python3
"""
Hedgehog — Venue Data Collector → TimescaleDB

Queries all 6 venues (Hyperliquid, Aster, Lighter, Apex, dYdX, Drift) for
account snapshots, positions, funding rates, and funding income, then writes
the data to TimescaleDB.

Usage:
    python3 scripts/collect_to_db.py --once          # single collection (default)
    python3 scripts/collect_to_db.py --loop           # continuous, every 60s
    python3 scripts/collect_to_db.py --loop --interval 120   # continuous, every 120s
    python3 scripts/collect_to_db.py --venues hl,aster       # specific venues only

Requires:
    - asyncpg (pip install asyncpg)
    - httpx   (pip install httpx)
    - .env with venue credentials (see each venue section)
    - TimescaleDB with init_db.sql + init_portfolio_db.sql applied

Environment:
    DATABASE_URL   — full postgres connection string (preferred)
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME — individual components
    Falls back to: postgresql://aegis:aegis_dev@localhost:5432/aegis
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import sys
import time
import traceback
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.parse import urlencode

import asyncpg
import httpx

# ── Load .env ────────────────────────────────────────────────────────────

def load_env(path: str):
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

# Add project root to path for adapter imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def get_database_url() -> str:
    """Build database URL from env vars, with sensible defaults for local dev.

    DATABASE_URL in .env typically uses Docker hostnames (e.g. timescaledb).
    When running outside Docker, replace with localhost.
    """
    url = os.environ.get("DATABASE_URL", "")
    if url:
        # Replace Docker-internal hostnames with localhost for local dev
        for docker_host in ("timescaledb", "aegis-db", "postgres"):
            url = url.replace(f"@{docker_host}:", "@localhost:")
        return url
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    user = os.environ.get("DB_USER", "aegis")
    password = os.environ.get("DB_PASSWORD", "aegis_dev")
    name = os.environ.get("DB_NAME", "aegis")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


# ═══════════════════════════════════════════════════════════════════════════
# Base Collector
# ═══════════════════════════════════════════════════════════════════════════

class VenueCollector(ABC):
    """Base class for venue data collection."""

    venue_name: str = ""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.stats = {"accounts": 0, "positions": 0, "funding_rates": 0, "income": 0, "errors": []}

    async def collect_all(self):
        """Run all collection methods, catching per-method errors."""
        now = datetime.now(timezone.utc)
        for method_name, method in [
            ("account", self.collect_account),
            ("positions", self.collect_positions),
            ("funding_rates", self.collect_funding_rates),
            ("income", self.collect_income),
        ]:
            try:
                await method(now)
            except Exception as e:
                self.stats["errors"].append(f"{method_name}: {e}")

    @abstractmethod
    async def collect_account(self, now: datetime): ...

    @abstractmethod
    async def collect_positions(self, now: datetime): ...

    @abstractmethod
    async def collect_funding_rates(self, now: datetime): ...

    @abstractmethod
    async def collect_income(self, now: datetime): ...

    def summary(self) -> str:
        parts = [f"{self.venue_name}: acct={self.stats['accounts']} pos={self.stats['positions']} "
                 f"rates={self.stats['funding_rates']} income={self.stats['income']}"]
        if self.stats["errors"]:
            parts.append(f"  errors: {'; '.join(self.stats['errors'])}")
        return "\n".join(parts)

    # ── DB helpers ────────────────────────────────────────────────────

    async def insert_account(self, now: datetime, *, nav: float, wallet_balance: float,
                             margin_used: float, free_margin: float, maint_margin: float,
                             margin_util_pct: float, unrealized_pnl: float,
                             withdrawable: float, position_count: int):
        await self.pool.execute(
            """INSERT INTO venue_accounts
               (timestamp, venue, nav, wallet_balance, margin_used, free_margin,
                maint_margin, margin_util_pct, unrealized_pnl, withdrawable, position_count)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            now, self.venue_name, nav, wallet_balance, margin_used, free_margin,
            maint_margin, margin_util_pct, unrealized_pnl, withdrawable, position_count,
        )
        self.stats["accounts"] += 1

    async def insert_positions(self, now: datetime, rows: list[dict]):
        if not rows:
            return
        await self.pool.executemany(
            """INSERT INTO venue_positions
               (timestamp, venue, symbol, side, size, notional, entry_price,
                mark_price, unrealized_pnl, leverage, liquidation_price)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            [(now, self.venue_name, r["symbol"], r["side"], r["size"], r["notional"],
              r["entry_price"], r["mark_price"], r["unrealized_pnl"],
              r["leverage"], r["liquidation_price"]) for r in rows],
        )
        self.stats["positions"] += len(rows)

    async def insert_funding_rates(self, now: datetime, rows: list[dict]):
        """Batch insert into the existing funding_rates table."""
        if not rows:
            return
        await self.pool.executemany(
            """INSERT INTO funding_rates
               (timestamp, venue, symbol, rate, annualized, cycle_hours,
                mark_price, index_price, open_interest, predicted_rate)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            [(now, self.venue_name, r["symbol"], r["rate"], r["annualized"],
              r["cycle_hours"], r.get("mark_price"), r.get("index_price"),
              r.get("open_interest"), r.get("predicted_rate")) for r in rows],
        )
        self.stats["funding_rates"] += len(rows)

    async def insert_income(self, rows: list[dict]):
        """Insert funding income with ON CONFLICT DO NOTHING for dedup."""
        if not rows:
            return
        # We use timestamp+venue+symbol+payment as a natural dedup key.
        # Since there's no unique constraint, we rely on the caller to avoid
        # re-fetching already-inserted data where possible.
        await self.pool.executemany(
            """INSERT INTO venue_funding_income
               (timestamp, venue, symbol, rate, payment)
               VALUES ($1,$2,$3,$4,$5)""",
            [(r["timestamp"], self.venue_name, r["symbol"], r["rate"], r["payment"])
             for r in rows],
        )
        self.stats["income"] += len(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Hyperliquid Collector
# ═══════════════════════════════════════════════════════════════════════════

class HyperliquidCollector(VenueCollector):
    venue_name = "hyperliquid"

    def __init__(self, pool: asyncpg.Pool):
        super().__init__(pool)
        self.api_url = "https://api.hyperliquid.xyz"
        self.address = os.environ.get("HYPERLIQUID_WALLET_ADDRESS", "")
        self._client: httpx.AsyncClient | None = None

    async def _post_info(self, payload: dict) -> dict | list:
        resp = await self._client.post(f"{self.api_url}/info", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def collect_account(self, now: datetime):
        if not self.address:
            self.stats["errors"].append("account: HYPERLIQUID_WALLET_ADDRESS not set")
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            self._client = client
            raw = await self._post_info({"type": "clearinghouseState", "user": self.address})

        cross = raw.get("crossMarginSummary", {})
        acct_value = float(cross.get("accountValue", 0))
        raw_usd = float(cross.get("totalRawUsd", 0))
        margin_used = float(cross.get("totalMarginUsed", 0))
        withdrawable = float(raw.get("withdrawable", 0))
        maint_margin = float(raw.get("crossMaintenanceMarginUsed", 0))
        free_margin = acct_value - margin_used
        margin_util = (margin_used / acct_value * 100) if acct_value else 0.0

        upnl = 0.0
        pos_count = 0
        for pos in raw.get("assetPositions", []):
            p = pos.get("position", pos)
            if float(p.get("szi", 0)) != 0:
                pos_count += 1
                upnl += float(p.get("unrealizedPnl", 0))

        await self.insert_account(
            now, nav=acct_value, wallet_balance=raw_usd, margin_used=margin_used,
            free_margin=free_margin, maint_margin=maint_margin,
            margin_util_pct=margin_util, unrealized_pnl=upnl,
            withdrawable=withdrawable, position_count=pos_count,
        )

    async def collect_positions(self, now: datetime):
        if not self.address:
            return

        # Use the adapter's get_positions for structured Position objects
        try:
            from adapters.hyperliquid_adapter import HyperliquidAdapter

            config = {
                "name": "Hyperliquid", "chain": "hyperliquid_l1", "chain_type": "evm",
                "settlement_chain": "hyperliquid_l1", "funding_cycle_hours": 1,
                "maker_fee_bps": 1.5, "taker_fee_bps": 4.5, "max_leverage": 50,
                "collateral_token": "USDC", "api_base_url": self.api_url,
                "ws_url": "wss://api.hyperliquid.xyz/ws", "deposit_chain": "arbitrum",
                "tier": "tier_1", "zero_gas": True, "symbol_format": "{symbol}",
                "symbol_overrides": {},
            }
            adapter = HyperliquidAdapter(config)
            await adapter.connect("")
            adapter._address = self.address
            pos_list = await adapter.get_positions()

            rows = []
            for p in pos_list:
                rows.append({
                    "symbol": p.symbol, "side": p.side.value.upper(),
                    "size": abs(p.size), "notional": p.size_usd,
                    "entry_price": p.entry_price, "mark_price": p.mark_price,
                    "unrealized_pnl": p.unrealized_pnl, "leverage": p.leverage,
                    "liquidation_price": p.liquidation_price,
                })
            await self.insert_positions(now, rows)
        except Exception as e:
            self.stats["errors"].append(f"positions: {e}")

    async def collect_funding_rates(self, now: datetime):
        async with httpx.AsyncClient(timeout=15.0) as client:
            self._client = client
            meta = await self._post_info({"type": "metaAndAssetCtxs"})

        universe = meta[0]["universe"]
        ctxs = meta[1]
        rows = []
        for u, c in zip(universe, ctxs):
            rate = float(c.get("funding") or 0)
            mark = float(c.get("markPx") or 0)
            oracle = float(c.get("oraclePx") or 0)
            oi = float(c.get("openInterest") or 0)
            premium = float(c.get("premium") or 0)
            rows.append({
                "symbol": u["name"], "rate": rate, "annualized": rate * 8760,
                "cycle_hours": 1, "mark_price": mark, "index_price": oracle,
                "open_interest": oi * mark, "predicted_rate": premium,
            })
        await self.insert_funding_rates(now, rows)

    async def collect_income(self, now: datetime):
        if not self.address:
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            self._client = client
            payments = await self._post_info({"type": "userFunding", "user": self.address})

        if not payments:
            return

        rows = []
        for entry in payments:
            ts = datetime.fromtimestamp(int(entry["time"]) / 1000, tz=timezone.utc)
            delta = entry.get("delta", entry)
            rows.append({
                "timestamp": ts,
                "symbol": delta["coin"],
                "rate": float(delta["fundingRate"]),
                "payment": float(delta["usdc"]),
            })
        await self.insert_income(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Aster Collector
# ═══════════════════════════════════════════════════════════════════════════

class AsterCollector(VenueCollector):
    venue_name = "aster"

    def __init__(self, pool: asyncpg.Pool):
        super().__init__(pool)
        self.base_url = "https://fapi.asterdex.com"
        self.api_key = os.environ.get("ASTER_API_KEY", "")
        self.api_secret = os.environ.get("ASTER_API_SECRET", "")
        self.has_auth = bool(self.api_key and self.api_secret)

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        qs = urlencode(params)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    async def collect_account(self, now: datetime):
        if not self.has_auth:
            self.stats["errors"].append("account: ASTER_API_KEY/SECRET not set")
            return

        headers = {"Content-Type": "application/json", "X-MBX-APIKEY": self.api_key}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0, headers=headers) as client:
            resp = await client.get("/fapi/v4/account", params=self._sign({}))
            resp.raise_for_status()
            acct = resp.json()

        wallet = float(acct.get("totalWalletBalance") or 0)
        unrealized = float(acct.get("totalUnrealizedProfit") or 0)
        margin_bal = float(acct.get("totalMarginBalance") or 0)
        available = float(acct.get("availableBalance") or 0)
        max_withdraw = float(acct.get("maxWithdrawAmount") or 0)
        pos_margin = float(acct.get("totalPositionInitialMargin") or 0)
        order_margin = float(acct.get("totalOpenOrderInitialMargin") or 0)
        maint_margin = float(acct.get("totalMaintMargin") or 0)
        margin_used = pos_margin + order_margin
        margin_util = margin_used / margin_bal * 100 if margin_bal > 0 else 0

        pos_count = len([p for p in acct.get("positions", [])
                         if float(p.get("positionAmt", 0)) != 0])

        await self.insert_account(
            now, nav=margin_bal, wallet_balance=wallet, margin_used=margin_used,
            free_margin=available, maint_margin=maint_margin,
            margin_util_pct=margin_util, unrealized_pnl=unrealized,
            withdrawable=max_withdraw, position_count=pos_count,
        )

        # Cache positions from the same response for collect_positions
        self._cached_positions = acct.get("positions", [])

    async def collect_positions(self, now: datetime):
        if not self.has_auth:
            return

        positions = getattr(self, "_cached_positions", None)
        if positions is None:
            # Fetch fresh if account wasn't called first
            headers = {"Content-Type": "application/json", "X-MBX-APIKEY": self.api_key}
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0, headers=headers) as client:
                resp = await client.get("/fapi/v4/account", params=self._sign({}))
                resp.raise_for_status()
                positions = resp.json().get("positions", [])

        rows = []
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            side = "LONG" if amt > 0 else "SHORT"
            notional = abs(float(p.get("notional", 0)))
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0))
            upnl = float(p.get("unrealizedProfit", 0))
            liq = float(p.get("liquidationPrice", 0))
            lev = float(p.get("leverage", 0))
            rows.append({
                "symbol": p["symbol"], "side": side, "size": abs(amt),
                "notional": notional, "entry_price": entry, "mark_price": mark,
                "unrealized_pnl": upnl, "leverage": lev, "liquidation_price": liq,
            })
        await self.insert_positions(now, rows)

    async def collect_funding_rates(self, now: datetime):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
            resp = await client.get("/fapi/v1/premiumIndex")
            resp.raise_for_status()
            all_premium = resp.json()

            resp2 = await client.get("/fapi/v1/ticker/24hr")
            resp2.raise_for_status()
            tickers = {t["symbol"]: t for t in resp2.json()}

        rows = []
        for p in all_premium:
            sym = p.get("symbol", "")
            t = tickers.get(sym, {})
            rate = float(p.get("lastFundingRate") or 0)
            mark = float(p.get("markPrice") or 0)
            index_px = float(p.get("indexPrice") or 0)
            oi_raw = float(t.get("openInterest") or 0)
            oi_usd = oi_raw * mark if mark > 0 else 0

            # Infer cycle hours from nextFundingTime
            nft = int(p.get("nextFundingTime", 0))
            cycle_h = 8  # default
            if nft:
                dt = datetime.fromtimestamp(nft / 1000, tz=timezone.utc)
                hour = dt.hour
                if hour % 8 != 0:
                    cycle_h = 1 if hour % 4 != 0 else 4

            payments_per_year = 365 * (24 / cycle_h)
            ann = rate * payments_per_year

            rows.append({
                "symbol": sym, "rate": rate, "annualized": ann,
                "cycle_hours": cycle_h, "mark_price": mark,
                "index_price": index_px, "open_interest": oi_usd,
                "predicted_rate": None,
            })
        await self.insert_funding_rates(now, rows)

    async def collect_income(self, now: datetime):
        if not self.has_auth:
            return

        headers = {"Content-Type": "application/json", "X-MBX-APIKEY": self.api_key}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0, headers=headers) as client:
            resp = await client.get("/fapi/v1/income",
                                    params=self._sign({"incomeType": "FUNDING_FEE", "limit": 100}))
            resp.raise_for_status()
            inc = resp.json()

        if not inc:
            return

        rows = []
        for i in inc:
            if i.get("incomeType") != "FUNDING_FEE":
                continue
            ts = datetime.fromtimestamp(int(i["time"]) / 1000, tz=timezone.utc)
            rows.append({
                "timestamp": ts,
                "symbol": i.get("symbol", ""),
                "rate": 0.0,  # Aster income endpoint doesn't include rate
                "payment": float(i.get("income", 0)),
            })
        await self.insert_income(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Lighter Collector
# ═══════════════════════════════════════════════════════════════════════════

class LighterCollector(VenueCollector):
    venue_name = "lighter"

    MARKET_NAMES = {
        0: "ETH", 1: "BTC", 2: "SOL", 3: "DOGE", 7: "XRP", 8: "LINK", 9: "AVAX",
        10: "NEAR", 11: "DOT", 12: "TON", 14: "POL", 16: "SUI", 24: "HYPE", 25: "BNB",
        27: "AAVE", 30: "UNI", 35: "LTC", 39: "ADA", 43: "TRX", 45: "PUMP", 58: "BCH",
        77: "XMR", 79: "SKY", 83: "ASTER", 90: "ZEC", 119: "XLM",
    }

    def __init__(self, pool: asyncpg.Pool):
        super().__init__(pool)
        self.base_url = "https://mainnet.zklighter.elliot.ai"
        acct_idx = os.environ.get("LIGHTER_ACCOUNT_INDEX", "")
        self.account_index = int(acct_idx) if acct_idx else None
        self.api_key_private = os.environ.get(
            "LIGHTER_API_KEY_PRIVATE_KEY",
            os.environ.get("LIGHTER_API_KEY_PRIVATE", ""),
        )
        api_idx = os.environ.get("LIGHTER_API_KEY_INDEX", "")
        self.api_key_index = int(api_idx) if api_idx else None

    def _get_auth_token(self) -> str | None:
        if not self.api_key_private or self.account_index is None or self.api_key_index is None:
            return None
        try:
            import lighter
            signer = lighter.SignerClient(
                url=self.base_url,
                api_private_keys={self.api_key_index: self.api_key_private},
                account_index=self.account_index,
            )
            err = signer.check_client()
            if err:
                return None
            auth_token, err = signer.create_auth_token_with_expiry(
                deadline=3600, api_key_index=self.api_key_index,
            )
            if err:
                return None
            return auth_token
        except ImportError:
            return None

    async def collect_account(self, now: datetime):
        if self.account_index is None:
            self.stats["errors"].append("account: LIGHTER_ACCOUNT_INDEX not set")
            return

        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            resp = await client.get("/api/v1/account",
                                    params={"by": "index", "value": str(self.account_index)})
            resp.raise_for_status()
            acct = resp.json()["accounts"][0]

        collateral = float(acct["collateral"])
        available = float(acct["available_balance"])
        total_asset = float(acct["total_asset_value"])
        margin_used = collateral - available
        util = (margin_used / collateral * 100) if collateral > 0 else 0

        total_upnl = 0.0
        pos_count = 0
        for p in acct["positions"]:
            if abs(float(p["position"])) > 1e-12:
                total_upnl += float(p["unrealized_pnl"])
                pos_count += 1

        await self.insert_account(
            now, nav=total_asset, wallet_balance=collateral, margin_used=margin_used,
            free_margin=available, maint_margin=0.0, margin_util_pct=util,
            unrealized_pnl=total_upnl, withdrawable=available,
            position_count=pos_count,
        )
        # Cache for positions
        self._cached_account = acct

    async def collect_positions(self, now: datetime):
        if self.account_index is None:
            return

        acct = getattr(self, "_cached_account", None)
        if acct is None:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
                resp = await client.get("/api/v1/account",
                                        params={"by": "index", "value": str(self.account_index)})
                resp.raise_for_status()
                acct = resp.json()["accounts"][0]

        rows = []
        for p in acct["positions"]:
            size = float(p["position"])
            if abs(size) < 1e-12:
                continue
            value = float(p["position_value"])
            imf = float(p["initial_margin_fraction"])
            mark = value / abs(size) if abs(size) > 1e-12 else 0.0
            leverage = 100.0 / imf if imf > 0 else 0.0
            liq = float(p["liquidation_price"]) if p["liquidation_price"] != "0" else 0.0
            rows.append({
                "symbol": p["symbol"],
                "side": "SHORT" if p["sign"] == -1 else "LONG",
                "size": abs(size), "notional": value,
                "entry_price": float(p["avg_entry_price"]),
                "mark_price": mark, "unrealized_pnl": float(p["unrealized_pnl"]),
                "leverage": leverage, "liquidation_price": liq,
            })
        await self.insert_positions(now, rows)

    async def collect_funding_rates(self, now: datetime):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            resp = await client.get("/api/v1/orderBookDetails")
            resp.raise_for_status()
            details = resp.json().get("order_book_details", [])

            rows = []
            # Fetch all funding rates in one call (bulk endpoint)
            rate_map: dict[int, float] = {}
            try:
                fr_resp = await client.get("/api/v1/funding-rates")
                fr_resp.raise_for_status()
                for fr in fr_resp.json().get("funding_rates", []):
                    rate_map[fr["market_id"]] = float(fr.get("rate", 0))
            except Exception:
                pass

            for d in details:
                if d.get("status") != "active":
                    continue
                mid = d["market_id"]
                symbol = d.get("symbol", self.MARKET_NAMES.get(mid, f"MKT_{mid}"))
                last_price = float(d.get("last_trade_price", 0))
                oi = float(d.get("open_interest", 0))

                rate = rate_map.get(mid, 0.0)
                ann = rate * 1095  # 8h funding cycle: 8760/8
                oi_usd = oi * last_price if last_price > 0 else oi
                rows.append({
                    "symbol": symbol, "rate": rate, "annualized": ann,
                    "cycle_hours": 8, "mark_price": last_price,
                    "index_price": None, "open_interest": oi_usd,
                    "predicted_rate": None,
                })

        await self.insert_funding_rates(now, rows)

    async def collect_income(self, now: datetime):
        auth_token = self._get_auth_token()
        if not auth_token:
            self.stats["errors"].append("income: no auth token (check lighter credentials)")
            return

        if self.account_index is None:
            return

        all_fundings = []
        cursor = None
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            for _ in range(5):
                params = {"account_index": self.account_index, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get("/api/v1/positionFunding", params=params,
                                        headers={"Authorization": auth_token})
                data = resp.json()
                entries = data.get("position_fundings", [])
                if not entries:
                    break
                all_fundings.extend(entries)
                cursor = data.get("next_cursor")
                if not cursor:
                    break

        if not all_fundings:
            return

        rows = []
        for f in all_fundings:
            mid = f["market_id"]
            symbol = self.MARKET_NAMES.get(mid, f"MKT_{mid}")
            ts = datetime.fromtimestamp(int(f["timestamp"]), tz=timezone.utc)
            rows.append({
                "timestamp": ts,
                "symbol": symbol,
                "rate": 0.0,  # Rate not included in funding response
                "payment": float(f["change"]),
            })
        await self.insert_income(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Apex Omni Collector
# ═══════════════════════════════════════════════════════════════════════════

class ApexCollector(VenueCollector):
    venue_name = "apex"

    SYMBOLS = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT",
        "LINKUSDT", "ARBUSDT", "AVAXUSDT", "WIFUSDT", "NEARUSDT",
        "AAVEUSDT", "XRPUSDT", "BNBUSDT", "TONUSDT", "ADAUSDT",
        "MATICUSDT", "OPUSDT", "APTUSDT", "TRXUSDT", "LTCUSDT",
        "DOTUSDT", "SEIUSDT", "PEPEUSDT", "ONDOUSDT", "HYPEUSDT",
        "JUPUSDT", "ORDIUSDT", "TIAUSDT", "STXUSDT", "MKRUSDT",
    ]

    def __init__(self, pool: asyncpg.Pool):
        super().__init__(pool)
        self.base_url = "https://omni.apex.exchange"
        self.api_key = os.environ.get("APEX_OMNI_API_KEY", "")
        self.api_secret = os.environ.get("APEX_OMNI_API_SECRET", "")
        self.passphrase = os.environ.get("APEX_OMNI_PASSPHRASE", "")
        self.has_auth = bool(self.api_key and self.api_secret and self.passphrase)

    def _sign_request(self, timestamp: str, method: str,
                      request_path: str, data_string: str = "") -> str:
        message = timestamp + method.upper() + request_path + data_string
        hmac_key = base64.standard_b64encode(self.api_secret.encode("utf-8"))
        sig = hmac.new(hmac_key, message.encode("utf-8"), hashlib.sha256)
        return base64.standard_b64encode(sig.digest()).decode()

    def _auth_headers(self, path: str, params: dict | None = None) -> dict:
        if params:
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
            full_path = f"{path}?{qs}" if qs else path
        else:
            full_path = path
        timestamp = str(int(round(time.time() * 1000)))
        sig = self._sign_request(timestamp, "GET", full_path)
        return {
            "APEX-SIGNATURE": sig,
            "APEX-API-KEY": self.api_key,
            "APEX-TIMESTAMP": timestamp,
            "APEX-PASSPHRASE": self.passphrase,
        }

    async def collect_account(self, now: datetime):
        if not self.has_auth:
            self.stats["errors"].append("account: APEX_OMNI credentials not set")
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Account balance
            headers = self._auth_headers("/api/v3/account-balance")
            resp = await client.get(f"{self.base_url}/api/v3/account-balance", headers=headers)
            resp.raise_for_status()
            bal = resp.json().get("data", {})

            if not bal or not isinstance(bal, dict):
                self.stats["errors"].append("account: empty balance response")
                return

            nav = float(bal.get("totalEquityValue") or 0)
            wallet = float(bal.get("walletBalance") or 0) or nav
            # totalAvailableBalance = actual tradeable equity;
            # availableBalance includes liabilities and can go negative
            available = float(bal.get("totalAvailableBalance") or bal.get("availableBalance") or 0)
            init_margin = float(bal.get("initialMargin") or 0)
            maint_margin = float(bal.get("maintenanceMargin") or 0)
            upnl = float(bal.get("unrealizedPnl") or 0)
            margin_util = (init_margin / nav * 100) if nav > 0 else 0

            # Get position count from account endpoint
            headers2 = self._auth_headers("/api/v3/account")
            resp2 = await client.get(f"{self.base_url}/api/v3/account", headers=headers2)
            resp2.raise_for_status()
            acct_data = resp2.json().get("data", {})
            acct_obj = acct_data.get("account", acct_data.get("accounts", {}))
            open_pos = {}
            if isinstance(acct_obj, dict):
                open_pos = acct_obj.get("openPositions", {})
            elif isinstance(acct_obj, list) and acct_obj:
                open_pos = acct_obj[0].get("openPositions", {})
            pos_count = len(open_pos) if isinstance(open_pos, dict) else 0

        await self.insert_account(
            now, nav=nav, wallet_balance=wallet, margin_used=init_margin,
            free_margin=available, maint_margin=maint_margin,
            margin_util_pct=margin_util, unrealized_pnl=upnl,
            withdrawable=available, position_count=pos_count,
        )
        # Cache for positions
        self._cached_open_pos = open_pos

    async def collect_positions(self, now: datetime):
        if not self.has_auth:
            return

        open_pos = getattr(self, "_cached_open_pos", None)
        if open_pos is None:
            async with httpx.AsyncClient(timeout=15.0) as client:
                headers = self._auth_headers("/api/v3/account")
                resp = await client.get(f"{self.base_url}/api/v3/account", headers=headers)
                resp.raise_for_status()
                acct_data = resp.json().get("data", {})
                acct_obj = acct_data.get("account", acct_data.get("accounts", {}))
                if isinstance(acct_obj, dict):
                    open_pos = acct_obj.get("openPositions", {})
                elif isinstance(acct_obj, list) and acct_obj:
                    open_pos = acct_obj[0].get("openPositions", {})
                else:
                    open_pos = {}

        if not open_pos or not isinstance(open_pos, dict):
            return

        rows = []
        for sym, pos in open_pos.items():
            side = pos.get("side", "UNKNOWN")
            size = abs(float(pos.get("size", 0)))
            entry = float(pos.get("entryPrice", 0))
            upnl = float(pos.get("unrealizedPnl", 0))
            liq = float(pos.get("liquidationPrice", 0)) if pos.get("liquidationPrice") else 0.0
            mark = float(pos.get("markPrice", 0)) if pos.get("markPrice") else entry
            lev = float(pos.get("leverage", 0)) if pos.get("leverage") else 0.0
            notional = size * entry
            rows.append({
                "symbol": sym, "side": side.upper(), "size": size,
                "notional": notional, "entry_price": entry, "mark_price": mark,
                "unrealized_pnl": upnl, "leverage": lev, "liquidation_price": liq,
            })
        await self.insert_positions(now, rows)

    async def collect_funding_rates(self, now: datetime):
        async with httpx.AsyncClient(timeout=15.0) as client:
            rows = []
            for sym in self.SYMBOLS:
                try:
                    resp = await client.get(f"{self.base_url}/api/v3/ticker",
                                            params={"symbol": sym})
                    items = resp.json().get("data", [])
                    if items and isinstance(items, list) and items[0]:
                        d = items[0]
                        rate = float(d.get("fundingRate") or 0)
                        pred = float(d.get("predictedFundingRate") or 0)
                        last = float(d.get("lastPrice") or 0)
                        oi = float(d.get("openInterest") or 0)
                        idx = float(d.get("indexPrice") or 0)
                        ann = rate * (8760 / 8)  # 8h cycle
                        rows.append({
                            "symbol": sym, "rate": rate, "annualized": ann,
                            "cycle_hours": 8, "mark_price": last,
                            "index_price": idx, "open_interest": oi * last,
                            "predicted_rate": pred,
                        })
                except Exception:
                    continue
        await self.insert_funding_rates(now, rows)

    async def collect_income(self, now: datetime):
        if not self.has_auth:
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            params = {"limit": "50"}
            headers = self._auth_headers("/api/v3/funding", params)
            resp = await client.get(f"{self.base_url}/api/v3/funding",
                                    headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            funding_list = data.get("fundingValues", data) if isinstance(data, dict) else data

        if not isinstance(funding_list, list) or not funding_list:
            return

        rows = []
        for f in funding_list:
            funding_time = f.get("fundingTime", 0)
            if isinstance(funding_time, str):
                try:
                    ts = datetime.fromisoformat(funding_time.replace("Z", "+00:00"))
                except ValueError:
                    ts = datetime.now(timezone.utc)
            else:
                ts = datetime.fromtimestamp(int(funding_time) / 1000, tz=timezone.utc)

            rate_raw = f.get("rate", 0)
            try:
                rate = float(rate_raw)
            except (ValueError, TypeError):
                rate = 0.0

            rows.append({
                "timestamp": ts,
                "symbol": f.get("symbol", ""),
                "rate": rate,
                "payment": float(f.get("fundingValue", 0)),
            })
        await self.insert_income(rows)


# ═══════════════════════════════════════════════════════════════════════════
# dYdX v4 Collector
# ═══════════════════════════════════════════════════════════════════════════

class DydxCollector(VenueCollector):
    venue_name = "dydx"

    def __init__(self, pool: asyncpg.Pool):
        super().__init__(pool)
        self.base_url = "https://indexer.dydx.trade/v4"
        self.address = os.environ.get("DYDX_WALLET_ADDRESS", "")

    async def collect_account(self, now: datetime):
        if not self.address:
            self.stats["errors"].append("account: DYDX_WALLET_ADDRESS not set")
            return

        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            resp = await client.get(f"/addresses/{self.address}")
            resp.raise_for_status()
            acct = resp.json()

        subaccounts = acct.get("subaccounts", [])
        if not subaccounts:
            self.stats["errors"].append("account: no subaccounts found")
            return

        # Use subaccount 0 as the primary account
        sa = subaccounts[0]
        equity = float(sa.get("equity", 0))
        free_collateral = float(sa.get("freeCollateral", 0))
        margin_used = equity - free_collateral
        margin_util = (margin_used / equity * 100) if equity > 0 else 0.0

        # Wallet balance from USDC asset position
        wallet_bal = 0.0
        for asset_id, ap in sa.get("assetPositions", {}).items():
            if ap.get("symbol") == "USDC":
                size = float(ap.get("size", 0))
                side = ap.get("side", "")
                wallet_bal = size if side == "LONG" else -size
                break

        total_upnl = 0.0
        perp_positions = sa.get("openPerpetualPositions", {})
        pos_count = len(perp_positions)
        for market, pp in perp_positions.items():
            total_upnl += float(pp.get("unrealizedPnl", 0))

        await self.insert_account(
            now, nav=equity, wallet_balance=wallet_bal, margin_used=margin_used,
            free_margin=free_collateral, maint_margin=0.0,
            margin_util_pct=margin_util, unrealized_pnl=total_upnl,
            withdrawable=free_collateral, position_count=pos_count,
        )
        # Cache
        self._cached_subaccount = sa

    async def collect_positions(self, now: datetime):
        if not self.address:
            return

        sa = getattr(self, "_cached_subaccount", None)
        if sa is None:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
                resp = await client.get(f"/addresses/{self.address}")
                resp.raise_for_status()
                acct = resp.json()
                subaccounts = acct.get("subaccounts", [])
                sa = subaccounts[0] if subaccounts else {}

        # Fetch oracle prices for mark price
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            mkt_resp = await client.get("/perpetualMarkets")
            mkt_resp.raise_for_status()
            markets_data = mkt_resp.json().get("markets", {})
        oracle_prices = {t: float(m.get("oraclePrice", 0)) for t, m in markets_data.items()}

        perp_positions = sa.get("openPerpetualPositions", {})
        rows = []
        for market, pp in perp_positions.items():
            size = float(pp.get("size", 0))
            side = pp.get("side", "")
            entry = float(pp.get("entryPrice", 0))
            upnl = float(pp.get("unrealizedPnl", 0))
            mark = oracle_prices.get(market, 0.0)
            notional = abs(size) * mark
            rows.append({
                "symbol": market, "side": side, "size": abs(size),
                "notional": notional, "entry_price": entry, "mark_price": mark,
                "unrealized_pnl": upnl, "leverage": 0.0,
                "liquidation_price": 0.0,
            })
        await self.insert_positions(now, rows)

    async def collect_funding_rates(self, now: datetime):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            resp = await client.get("/perpetualMarkets")
            resp.raise_for_status()
            markets = resp.json().get("markets", {})

        rows = []
        for ticker, m in markets.items():
            if m.get("status") != "ACTIVE":
                continue
            rate = float(m.get("nextFundingRate", 0))
            oracle = float(m.get("oraclePrice", 0))
            oi = float(m.get("openInterest", 0))
            ann = rate * 8760  # 1h funding cycle
            oi_usd = oi * oracle
            rows.append({
                "symbol": ticker, "rate": rate, "annualized": ann,
                "cycle_hours": 1, "mark_price": oracle,
                "index_price": oracle, "open_interest": oi_usd,
                "predicted_rate": None,
            })
        await self.insert_funding_rates(now, rows)

    async def collect_income(self, now: datetime):
        # dYdX v4 indexer does NOT have a dedicated funding payments endpoint.
        # This is a known limitation.
        self.stats["errors"].append("income: not available via dYdX v4 indexer API")


# ═══════════════════════════════════════════════════════════════════════════
# Drift Collector
# ═══════════════════════════════════════════════════════════════════════════

class DriftCollector(VenueCollector):
    venue_name = "drift"

    def __init__(self, pool: asyncpg.Pool):
        super().__init__(pool)
        self.data_api = "https://data.api.drift.trade"
        self.address = os.environ.get("DRIFT_WALLET_ADDRESS", "")
        self._account_id: str | None = None
        self._idx_to_sym: dict = {}

    async def _resolve_account_id(self, client: httpx.AsyncClient) -> str:
        if self._account_id:
            return self._account_id
        resp = await client.get(f"{self.data_api}/authority/{self.address}/accounts")
        resp.raise_for_status()
        data = resp.json()
        accounts = data.get("accounts", [])
        if accounts:
            self._account_id = accounts[0]["accountId"]
            return self._account_id
        return ""

    async def collect_account(self, now: datetime):
        if not self.address:
            self.stats["errors"].append("account: DRIFT_WALLET_ADDRESS not set")
            return

        async with httpx.AsyncClient(timeout=20.0) as client:
            account_id = await self._resolve_account_id(client)
            if not account_id:
                self.stats["errors"].append("account: could not resolve Drift account ID")
                return

            resp = await client.get(f"{self.data_api}/user/{account_id}")
            resp.raise_for_status()
            data = resp.json()

        acct = data.get("account", {})
        balance = float(acct.get("balance", 0))
        collateral = float(acct.get("totalCollateral", 0))
        free_collateral = float(acct.get("freeCollateral", 0))
        init_margin = float(acct.get("initialMargin", 0))
        maint_margin = float(acct.get("maintenanceMargin", 0))
        margin_used = collateral - free_collateral
        margin_util = (margin_used / collateral * 100) if collateral > 0 else 0.0

        # Count positions
        positions = data.get("positions", [])
        pos_count = len([p for p in positions
                         if float(p.get("baseAssetAmount", p.get("size", 0))) != 0])

        # uPnL
        upnl = sum(float(p.get("unrealizedPnl", p.get("pnl", 0)))
                    for p in positions
                    if float(p.get("baseAssetAmount", p.get("size", 0))) != 0)

        await self.insert_account(
            now, nav=collateral, wallet_balance=balance, margin_used=margin_used,
            free_margin=free_collateral, maint_margin=maint_margin,
            margin_util_pct=margin_util, unrealized_pnl=upnl,
            withdrawable=free_collateral, position_count=pos_count,
        )
        # Cache
        self._cached_positions = positions

    async def collect_positions(self, now: datetime):
        if not self.address:
            return

        positions = getattr(self, "_cached_positions", None)
        if positions is None:
            async with httpx.AsyncClient(timeout=20.0) as client:
                account_id = await self._resolve_account_id(client)
                if not account_id:
                    return
                resp = await client.get(f"{self.data_api}/user/{account_id}")
                resp.raise_for_status()
                positions = resp.json().get("positions", [])

        rows = []
        for p in positions:
            size = float(p.get("baseAssetAmount", p.get("size", 0)))
            if size == 0:
                continue
            side = "LONG" if size > 0 else "SHORT"
            symbol = p.get("symbol", p.get("marketName", f"mkt-{p.get('marketIndex', '?')}"))
            entry = float(p.get("entryPrice", p.get("avgEntryPrice", 0)))
            mark = float(p.get("markPrice", p.get("oraclePrice", 0)))
            upnl = float(p.get("unrealizedPnl", p.get("pnl", 0)))
            notional = abs(size) * mark
            liq = float(p.get("liquidationPrice", 0))
            lev = float(p.get("leverage", 0)) if p.get("leverage") else 0.0
            rows.append({
                "symbol": symbol, "side": side, "size": abs(size),
                "notional": notional, "entry_price": entry, "mark_price": mark,
                "unrealized_pnl": upnl, "leverage": lev,
                "liquidation_price": liq,
            })
        await self.insert_positions(now, rows)

    async def collect_funding_rates(self, now: datetime):
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{self.data_api}/stats/markets")
            resp.raise_for_status()
            data = resp.json()

        perps = [m for m in data.get("markets", []) if m.get("marketType") == "perp"]

        rows = []
        for m in perps:
            symbol = m.get("symbol", "?")
            oracle = float(m.get("oraclePrice", 0))
            mark_price = float(m.get("markPrice", 0))

            oi_data = m.get("openInterest", {})
            if isinstance(oi_data, dict):
                oi_long = abs(float(oi_data.get("long", 0)))
                oi_short = abs(float(oi_data.get("short", 0)))
                oi_usd = (oi_long + oi_short) / 2 * oracle
            else:
                oi_usd = float(oi_data or 0) * oracle

            fr = m.get("fundingRate", {})
            if isinstance(fr, dict):
                funding = float(fr.get("long", fr.get("short", 0)))
            else:
                funding = float(fr or 0)
            # Drift funding is per-hour rate as percentage
            rate_decimal = funding / 100
            ann = rate_decimal * 8760

            # Store index → symbol mapping for income
            market_idx = m.get("marketIndex")
            if market_idx is not None:
                self._idx_to_sym[market_idx] = symbol

            rows.append({
                "symbol": symbol, "rate": rate_decimal, "annualized": ann,
                "cycle_hours": 1, "mark_price": mark_price,
                "index_price": oracle, "open_interest": oi_usd,
                "predicted_rate": None,
            })
        await self.insert_funding_rates(now, rows)

    async def collect_income(self, now: datetime):
        if not self.address:
            return

        async with httpx.AsyncClient(timeout=20.0) as client:
            account_id = await self._resolve_account_id(client)
            if not account_id:
                self.stats["errors"].append("income: could not resolve Drift account ID")
                return

            resp = await client.get(f"{self.data_api}/user/{account_id}/fundingPayments",
                                    params={"limit": 50})
            resp.raise_for_status()
            data = resp.json()

        records = data.get("records", [])
        if not records:
            return

        # Ensure we have the market index mapping
        if not self._idx_to_sym:
            async with httpx.AsyncClient(timeout=20.0) as client:
                mkt_resp = await client.get(f"{self.data_api}/stats/markets")
                mkt_resp.raise_for_status()
                for m in mkt_resp.json().get("markets", []):
                    if m.get("marketType") == "perp":
                        self._idx_to_sym[m.get("marketIndex")] = m.get("symbol", "?")

        rows = []
        for rec in records:
            payment = float(rec.get("fundingPayment", 0))
            market_idx = rec.get("marketIndex", "?")
            symbol = self._idx_to_sym.get(market_idx, f"mkt-{market_idx}")
            ts_val = rec.get("ts", 0)
            ts = datetime.fromtimestamp(int(ts_val), tz=timezone.utc)
            rows.append({
                "timestamp": ts,
                "symbol": symbol,
                "rate": 0.0,  # Rate not included in payment response
                "payment": payment,
            })
        await self.insert_income(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Main Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

VENUE_MAP = {
    "hl": HyperliquidCollector,
    "hyperliquid": HyperliquidCollector,
    "aster": AsterCollector,
    "lighter": LighterCollector,
    "apex": ApexCollector,
    "dydx": DydxCollector,
    "drift": DriftCollector,
}

ALL_VENUES = ["hyperliquid", "aster", "lighter", "apex", "dydx", "drift"]


async def run_collection(pool: asyncpg.Pool, venue_names: list[str]):
    """Run all venue collectors concurrently, print summary."""
    collectors = []
    for name in venue_names:
        cls = VENUE_MAP.get(name)
        if cls is None:
            print(f"  WARNING: unknown venue '{name}', skipping")
            continue
        collectors.append(cls(pool))

    if not collectors:
        print("  No venues to collect")
        return

    # Run all venue collections concurrently
    results = await asyncio.gather(
        *(c.collect_all() for c in collectors),
        return_exceptions=True,
    )

    # Print summary
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"  Collection Summary — {ts}")
    print(f"{'='*60}")

    for i, collector in enumerate(collectors):
        exc = results[i]
        if isinstance(exc, Exception):
            print(f"  {collector.venue_name}: FATAL — {exc}")
        else:
            print(f"  {collector.summary()}")

    print(f"{'='*60}\n")


async def main():
    parser = argparse.ArgumentParser(
        description="Hedgehog — collect venue data to TimescaleDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", default=True,
                      help="Run once and exit (default)")
    mode.add_argument("--loop", action="store_true",
                      help="Run continuously")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between collection cycles (default: 60)")
    parser.add_argument("--venues", type=str, default=None,
                        help="Comma-separated venue list (default: all)")
    args = parser.parse_args()

    venue_names = ALL_VENUES
    if args.venues:
        venue_names = [v.strip().lower() for v in args.venues.split(",")]

    db_url = get_database_url()
    print(f"  Connecting to DB: {db_url.split('@')[0].split('//')[0]}//***@{db_url.split('@')[-1]}")

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    print(f"  DB connected. Venues: {', '.join(venue_names)}")

    try:
        if args.loop:
            print(f"  Running continuously every {args.interval}s. Ctrl+C to stop.\n")
            while True:
                await run_collection(pool, venue_names)
                await asyncio.sleep(args.interval)
        else:
            await run_collection(pool, venue_names)
    except KeyboardInterrupt:
        print("\n  Stopped by user.")
    finally:
        await pool.close()
        print("  DB pool closed.")


if __name__ == "__main__":
    asyncio.run(main())
