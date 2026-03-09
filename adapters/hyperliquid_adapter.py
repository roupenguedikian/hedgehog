"""
hedgehog/adapters/hyperliquid_adapter.py
Hyperliquid L1 adapter using the official Python SDK + raw REST/WS fallbacks.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Optional

import asyncio

import httpx
import structlog

from adapters.base_adapter import BaseDefiAdapter
from models.core import (
    FundingRate, Orderbook, OrderbookLevel, Position, OrderResult,
    Side, OrderStatus, VenueConfig,
)

logger = structlog.get_logger()

API_URL = "https://api.hyperliquid.xyz"


class HyperliquidAdapter(BaseDefiAdapter):
    """
    Hyperliquid adapter.

    Uses raw REST calls for data (no SDK dependency for portability)
    and the SDK for authenticated trading operations.
    """

    def __init__(self, config: VenueConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._exchange = None  # Hyperliquid SDK Exchange, cached after connect()
        self._address: str = ""
        self._private_key: str = ""
        self._meta_cache: Optional[dict] = None
        self._asset_map: dict[str, int] = {}

    async def connect(self, private_key: str, **kwargs) -> bool:
        try:
            self._client = httpx.AsyncClient(base_url=API_URL, timeout=15.0)

            if private_key:
                from eth_account import Account
                acct = Account.from_key(private_key)
                self._address = acct.address
                self._private_key = private_key

                from hyperliquid.exchange import Exchange
                from hyperliquid.utils import constants
                self._exchange = Exchange(
                    wallet=self._private_key,
                    base_url=constants.MAINNET_API_URL,
                )

            # Fetch and cache metadata (asset universe) — public endpoint
            resp = await self._client.post("/info", json={"type": "metaAndAssetCtxs"})
            resp.raise_for_status()
            data = resp.json()
            self._meta_cache = data
            # Build asset name -> index mapping
            universe = data[0]["universe"]
            self._asset_map = {a["name"]: i for i, a in enumerate(universe)}
            self.connected = True
            logger.info("hyperliquid.connected",
                        address=self._address or "read-only",
                        assets=len(self._asset_map))
            return True
        except Exception as e:
            logger.error("hyperliquid.connect_failed", error=str(e))
            return False

    async def _post_info(self, payload: dict, retries: int = 3) -> dict:
        for attempt in range(retries):
            resp = await self._client.post("/info", json=payload)
            if resp.status_code in (429, 502, 503, 504) and attempt < retries - 1:
                wait = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s
                logger.warning("hyperliquid.transient_error",
                               status=resp.status_code, attempt=attempt + 1, retry_in=wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()

    # ── Market Data ──────────────────────────────────────────────────────

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        data = await self._post_info({"type": "metaAndAssetCtxs"})
        idx = self._asset_map.get(symbol)
        if idx is None:
            raise ValueError(f"Symbol {symbol} not found on Hyperliquid")

        ctx = data[1][idx]
        rate = float(ctx["funding"])
        premium = float(ctx.get("premium", "0"))
        mark_price = float(ctx["markPx"])
        oracle_price = float(ctx["oraclePx"])

        return FundingRate(
            venue="hyperliquid",
            symbol=symbol,
            rate=rate,
            cycle_hours=1,
            mark_price=mark_price,
            index_price=oracle_price,
            predicted_rate=premium,
            timestamp=datetime.now(timezone.utc),
        )

    async def get_funding_history(
        self, symbol: str, start_time: Optional[int] = None, limit: int = 100
    ) -> list[FundingRate]:
        if start_time is None:
            start_time = int((time.time() - 7 * 86400) * 1000)  # 7 days ago

        data = await self._post_info({
            "type": "fundingHistory",
            "coin": symbol,
            "startTime": start_time,
        })

        rates = []
        for entry in data[-limit:]:
            rates.append(FundingRate(
                venue="hyperliquid",
                symbol=entry["coin"],
                rate=float(entry["fundingRate"]),
                cycle_hours=1,
                timestamp=datetime.fromtimestamp(entry["time"] / 1000, tz=timezone.utc),
            ))
        return rates

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        data = await self._post_info({"type": "l2Book", "coin": symbol, "nSigFigs": 5})
        levels = data.get("levels", [[], []])

        bids = [OrderbookLevel(price=float(l["px"]), size=float(l["sz"])) for l in levels[0][:depth]]
        asks = [OrderbookLevel(price=float(l["px"]), size=float(l["sz"])) for l in levels[1][:depth]]

        return Orderbook(venue="hyperliquid", symbol=symbol, bids=bids, asks=asks)

    # ── Trading ──────────────────────────────────────────────────────────

    async def place_limit_order(
        self, symbol: str, side: Side, size: float, price: float,
        reduce_only: bool = False, tif: str = "GTC",
    ) -> OrderResult:
        """Place limit order via Hyperliquid SDK."""
        try:
            if not self._exchange:
                raise RuntimeError("Exchange not initialized — connect() with a private key first")

            is_buy = side == Side.LONG
            tif_map = {"GTC": "Gtc", "IOC": "Ioc", "ALO": "Alo"}

            result = self._exchange.order(
                symbol, is_buy, size, price,
                {"limit": {"tif": tif_map.get(tif, "Gtc")}},
                reduce_only=reduce_only,
            )

            status = result.get("status", "")
            if status == "ok":
                fill = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
                return OrderResult(
                    venue="hyperliquid", symbol=symbol, side=side,
                    status=OrderStatus.FILLED if "filled" in fill else OrderStatus.SUBMITTED,
                    order_id=str(fill.get("resting", {}).get("oid", "")),
                    filled_qty=float(fill.get("filled", {}).get("totalSz", 0)),
                    avg_price=float(fill.get("filled", {}).get("avgPx", 0)),
                )
            else:
                return OrderResult(
                    venue="hyperliquid", symbol=symbol, side=side,
                    status=OrderStatus.FAILED, error=str(result),
                )
        except Exception as e:
            logger.error("hyperliquid.order_failed", error=str(e))
            return OrderResult(
                venue="hyperliquid", symbol=symbol, side=side,
                status=OrderStatus.FAILED, error=str(e),
            )

    async def place_market_order(
        self, symbol: str, side: Side, size: float, reduce_only: bool = False,
    ) -> OrderResult:
        """Market order via aggressive IOC limit."""
        ob = await self.get_orderbook(symbol, depth=5)
        if side == Side.LONG:
            # Buy: price above best ask
            price = ob.asks[0].price * 1.005 if ob.asks else 0
        else:
            # Sell: price below best bid
            price = ob.bids[0].price * 0.995 if ob.bids else 0

        return await self.place_limit_order(symbol, side, size, price, reduce_only, tif="IOC")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            if not self._exchange:
                raise RuntimeError("Exchange not initialized — connect() with a private key first")
            result = self._exchange.cancel(symbol, int(order_id))
            return result.get("status") == "ok"
        except Exception as e:
            logger.error("hyperliquid.cancel_failed", error=str(e))
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        try:
            if not self._exchange:
                raise RuntimeError("Exchange not initialized — connect() with a private key first")
            open_orders = await self._post_info({"type": "openOrders", "user": self._address})
            count = 0
            for order in open_orders:
                if symbol and order.get("coin") != symbol:
                    continue
                self._exchange.cancel(order["coin"], order["oid"])
                count += 1
            return count
        except Exception as e:
            logger.error("hyperliquid.cancel_all_failed", error=str(e))
            return 0

    # ── Account ──────────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        data = await self._post_info({"type": "clearinghouseState", "user": self._address})
        positions = []
        for pos_data in data.get("assetPositions", []):
            p = pos_data["position"]
            szi = float(p["szi"])
            if szi == 0:
                continue
            positions.append(Position(
                venue="hyperliquid",
                symbol=p["coin"],
                side=Side.LONG if szi > 0 else Side.SHORT,
                size=abs(szi),
                size_usd=float(p.get("positionValue", 0)),
                entry_price=float(p["entryPx"]),
                mark_price=float(p.get("markPx", 0)),
                unrealized_pnl=float(p["unrealizedPnl"]),
                margin=float(p.get("marginUsed", 0)),
                leverage=float(p.get("leverage", {}).get("value", 1)),
                funding_accrued=float(p.get("cumFunding", {}).get("sinceOpen", 0)),
                liquidation_price=float(p.get("liquidationPx") or 0),
            ))
        return positions

    async def get_balance(self) -> dict:
        data = await self._post_info({"type": "clearinghouseState", "user": self._address})
        cross = data.get("crossMarginSummary", {})
        return {
            "available": float(cross.get("availableBalance", 0)),
            "total": float(cross.get("accountValue", 0)),
            "margin_used": float(cross.get("totalMarginUsed", 0)),
        }

    def normalize_symbol(self, symbol: str) -> str:
        """Hyperliquid uses bare symbols: BTC, ETH, SOL."""
        return symbol
