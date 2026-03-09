"""
hedgehog/adapters/base_adapter.py
Abstract base for all DeFi perpetual venue adapters.
"""
from __future__ import annotations
import asyncio
from abc import ABC, abstractmethod
from typing import Optional
import structlog
from models.core import (
    FundingRate, Orderbook, Position, OrderResult, Side, VenueConfig,
)

logger = structlog.get_logger()


class BaseDefiAdapter(ABC):
    def __init__(self, config: VenueConfig):
        self.config = config
        self.name = config.name
        self.connected = False
        self._semaphore = asyncio.Semaphore(10)

    @abstractmethod
    async def connect(self, private_key: str, **kwargs) -> bool: ...

    async def disconnect(self):
        self.connected = False

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate: ...

    @abstractmethod
    async def get_funding_history(self, symbol: str, start_time: Optional[int] = None, limit: int = 100) -> list[FundingRate]: ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook: ...

    async def get_mark_price(self, symbol: str) -> float:
        ob = await self.get_orderbook(symbol, depth=1)
        if ob.bids and ob.asks:
            return (ob.bids[0].price + ob.asks[0].price) / 2
        return 0.0

    @abstractmethod
    async def place_limit_order(self, symbol: str, side: Side, size: float, price: float,
                                 reduce_only: bool = False, tif: str = "GTC") -> OrderResult: ...

    @abstractmethod
    async def place_market_order(self, symbol: str, side: Side, size: float,
                                  reduce_only: bool = False) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_balance(self) -> dict: ...

    async def get_deposit_info(self) -> dict:
        return {"chain": self.config.deposit_chain, "token": self.config.collateral_token}

    def normalize_symbol(self, symbol: str) -> str:
        overrides = getattr(self.config, 'symbol_overrides', None) or {}
        if symbol in overrides:
            return overrides[symbol]
        return self.config.symbol_format.replace("{symbol}", symbol)

    def estimate_gas_cost(self, operation: str = "trade") -> float:
        if self.config.zero_gas:
            return 0.0
        return {"evm": 0.02, "solana": 0.01, "cosmos": 0.01, "starknet": 0.005}.get(
            self.config.chain_type.value, 0.05
        )

    def round_trip_fee_bps(self) -> float:
        return (self.config.maker_fee_bps + self.config.taker_fee_bps) * 2
