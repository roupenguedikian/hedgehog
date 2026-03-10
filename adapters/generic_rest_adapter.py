"""
hedgehog/adapters/generic_rest_adapter.py
Generic adapter for REST/WS-based perp DEXs.
Used as base for: Aster (Binance-compat), Lighter, Ethereal, ApeX, Paradex.
Subclass and override specifics.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

from adapters.base_adapter import BaseDefiAdapter
from models.core import (
    FundingRate, Orderbook, OrderbookLevel, Position, OrderResult,
    Side, OrderStatus, VenueConfig,
)

logger = structlog.get_logger()


class GenericRestAdapter(BaseDefiAdapter):
    """
    REST-based adapter. Override `_build_headers`, `_sign_request`,
    and endpoint paths for each venue.
    """

    def __init__(self, config: VenueConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._private_key: str = ""
        self._address: str = ""

    async def connect(self, private_key: str, **kwargs) -> bool:
        self._private_key = private_key
        self._client = httpx.AsyncClient(
            base_url=self.config.api_base_url,
            timeout=15.0,
            headers=self._build_headers(),
        )
        self.connected = True
        logger.info(f"{self.config.name.lower()}.connected")
        return True

    def _build_headers(self) -> dict:
        return {"Content-Type": "application/json"}

    # ── Market Data (override paths per venue) ───────────────────────────

    def _funding_endpoint(self, symbol: str) -> str:
        return f"/fapi/v1/premiumIndex?symbol={symbol}"

    def _orderbook_endpoint(self, symbol: str, depth: int) -> str:
        return f"/fapi/v1/depth?symbol={symbol}&limit={depth}"

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        vsymbol = self.normalize_symbol(symbol)
        resp = await self._client.get(self._funding_endpoint(vsymbol))
        resp.raise_for_status()
        data = resp.json()

        rate = float(data.get("lastFundingRate", 0))
        mark = float(data.get("markPrice", 0))
        index = float(data.get("indexPrice", 0))
        next_ts = int(data.get("nextFundingTime", 0))

        return FundingRate(
            venue=self.config.name.lower(),
            symbol=symbol,
            rate=rate,
            cycle_hours=self.config.funding_cycle_hours,
            mark_price=mark,
            index_price=index,
            next_funding_ts=datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc) if next_ts else None,
        )

    async def get_funding_history(
        self, symbol: str, start_time: Optional[int] = None, limit: int = 100
    ) -> list[FundingRate]:
        vsymbol = self.normalize_symbol(symbol)
        params = {"symbol": vsymbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        resp = await self._client.get("/fapi/v1/fundingRate", params=params)
        resp.raise_for_status()
        data = resp.json()

        return [
            FundingRate(
                venue=self.config.name.lower(),
                symbol=symbol,
                rate=float(entry["fundingRate"]),
                cycle_hours=self.config.funding_cycle_hours,
                timestamp=datetime.fromtimestamp(entry["fundingTime"] / 1000, tz=timezone.utc),
            )
            for entry in data
        ]

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        vsymbol = self.normalize_symbol(symbol)
        resp = await self._client.get(self._orderbook_endpoint(vsymbol, depth))
        resp.raise_for_status()
        data = resp.json()

        bids = [OrderbookLevel(price=float(b[0]), size=float(b[1])) for b in data.get("bids", [])]
        asks = [OrderbookLevel(price=float(a[0]), size=float(a[1])) for a in data.get("asks", [])]

        return Orderbook(venue=self.config.name.lower(), symbol=symbol, bids=bids, asks=asks)

    # ── Trading (stubs — override with signing logic per venue) ──────────

    async def place_limit_order(self, symbol: str, side: Side, size: float, price: float,
                                 reduce_only: bool = False, tif: str = "GTC") -> OrderResult:
        logger.warning(f"{self.config.name}.place_limit_order: stub — override in subclass")
        return OrderResult(venue=self.config.name.lower(), symbol=symbol, side=side,
                           status=OrderStatus.FAILED, error="Not implemented")

    async def place_market_order(self, symbol: str, side: Side, size: float,
                                  reduce_only: bool = False) -> OrderResult:
        logger.warning(f"{self.config.name}.place_market_order: stub — override in subclass")
        return OrderResult(venue=self.config.name.lower(), symbol=symbol, side=side,
                           status=OrderStatus.FAILED, error="Not implemented")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        return 0

    async def get_positions(self) -> list[Position]:
        return []

    async def get_balance(self) -> dict:
        return {"available": 0, "total": 0, "margin_used": 0}


class LighterAdapter(GenericRestAdapter):
    """Lighter — ZK-rollup with custom REST API."""
    def _funding_endpoint(self, symbol: str) -> str:
        return f"/api/v1/funding-rate?market={symbol}"

    def _orderbook_endpoint(self, symbol: str, depth: int) -> str:
        return f"/api/v1/orderbook?market={symbol}&depth={depth}"


class EtherealAdapter(GenericRestAdapter):
    """Ethereal — Converge appchain, USDe collateral."""
    pass


class ApexAdapter(GenericRestAdapter):
    """ApeX Omni — zkLink-based multi-chain."""
    def _funding_endpoint(self, symbol: str) -> str:
        return f"/ticker?symbol={symbol}"
