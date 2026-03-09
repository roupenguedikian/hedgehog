"""
hedgehog/services/data/coinglass_client.py
CoinGlass API client for aggregated funding rate data across exchanges.
Also integrates DefiLlama for volume/TVL data.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

from models.core import FundingRate

logger = structlog.get_logger()

COINGLASS_BASE = "https://open-api-v3.coinglass.com"
DEFILLAMA_PERPS = "https://api.llama.fi/overview/derivatives"


class CoinglassClient:
    """
    Fetches aggregated funding rate data from CoinGlass.
    
    CoinGlass covers most major CEX + some DEX venues. We use it for:
    - Cross-exchange funding rate comparison
    - Funding rate arbitrage opportunity detection
    - Open interest data
    - Historical rates for backtesting
    
    For DEX venues not on CoinGlass, we supplement with direct API calls.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("COINGLASS_API_KEY", "")
        self._client = httpx.AsyncClient(
            base_url=COINGLASS_BASE,
            timeout=15.0,
            headers={
                "accept": "application/json",
                "CG-API-KEY": self.api_key,
            },
        )

    async def get_current_funding_rates(self, symbol: str = "BTC") -> list[dict]:
        """
        Get current funding rates across all exchanges for a symbol.
        Returns list of {exchange, rate, annualized, nextFundingTime, ...}
        """
        try:
            resp = await self._client.get(
                "/api/futures/funding/current",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != "0":
                logger.warning("coinglass.funding_error", msg=data.get("msg"))
                return []

            results = []
            for item in data.get("data", []):
                for market in item.get("marginList", []):
                    exchange = market.get("exchangeName", "")
                    rate = float(market.get("rate", 0))
                    results.append({
                        "exchange": exchange,
                        "symbol": symbol,
                        "rate": rate,
                        "annualized_pct": rate * 3 * 365 * 100,  # 8h default
                        "next_funding_time": market.get("nextFundingTime"),
                        "open_interest": float(market.get("openInterest", 0)),
                    })
            return results

        except Exception as e:
            logger.error("coinglass.funding_fetch_failed", symbol=symbol, error=str(e))
            return []

    async def get_funding_arbitrage(self) -> list[dict]:
        """
        Get funding rate arbitrage opportunities from CoinGlass.
        Returns pairs of exchanges with the highest funding rate spreads.
        """
        try:
            resp = await self._client.get("/api/futures/fundingRate/arbitrage")
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as e:
            logger.error("coinglass.arbitrage_fetch_failed", error=str(e))
            return []

    async def get_funding_history(
        self, symbol: str = "BTC", exchange: str = "Hyperliquid", interval: str = "h8"
    ) -> list[dict]:
        """
        Get historical funding rates for a specific exchange + symbol.
        interval: h1, h4, h8
        """
        try:
            resp = await self._client.get(
                "/api/futures/funding/history",
                params={"symbol": symbol, "exchange": exchange, "interval": interval},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as e:
            logger.error("coinglass.history_failed", error=str(e))
            return []

    async def get_open_interest(self, symbol: str = "BTC") -> list[dict]:
        """Get open interest across exchanges."""
        try:
            resp = await self._client.get(
                "/api/futures/openInterest/current",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as e:
            logger.error("coinglass.oi_failed", error=str(e))
            return []

    async def close(self):
        await self._client.aclose()


class DefiLlamaClient:
    """
    DefiLlama API for perp DEX volume and TVL rankings.
    Used for liquidity scoring in the venue scorer.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=15.0)

    async def get_perp_volumes(self) -> dict[str, dict]:
        """
        Get 24h perp volumes for all DEXs.
        Returns: {protocol_name: {volume_24h, change_1d, ...}}
        """
        try:
            resp = await self._client.get(DEFILLAMA_PERPS)
            resp.raise_for_status()
            data = resp.json()

            result = {}
            for protocol in data.get("protocols", []):
                name = protocol.get("name", "").lower()
                result[name] = {
                    "volume_24h": float(protocol.get("total24h") or 0),
                    "volume_7d": float(protocol.get("total7d") or 0),
                    "change_1d": float(protocol.get("change_1d") or 0),
                    "chains": protocol.get("chains", []),
                }
            return result

        except Exception as e:
            logger.error("defillama.volumes_failed", error=str(e))
            return {}

    async def get_protocol_tvl(self, protocol_slug: str) -> float:
        """Get TVL for a specific protocol."""
        try:
            resp = await self._client.get(f"https://api.llama.fi/tvl/{protocol_slug}")
            resp.raise_for_status()
            return float(resp.text)
        except Exception:
            return 0.0

    async def close(self):
        await self._client.aclose()
